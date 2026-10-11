"""Platform defaults and the per-platform implementations behind platform_compat.

The Windows branches run here against a faked platform and a fake `msvcrt`; the CI matrix runs the real ones."""
import errno
import json
import os
import subprocess
import sys
import time

import pytest

from quota_router import platform_compat as pc


def test_state_dir_defaults_per_platform(home):
    assert pc.default_state_dir("linux", {}) == str(home / ".local" / "state" / "quota-router")
    assert pc.default_state_dir("darwin", {}) == str(home / ".local" / "state" / "quota-router")
    assert pc.default_state_dir("linux", {"XDG_STATE_HOME": "/x/state"}) == os.path.join("/x/state", "quota-router")
    assert pc.default_state_dir("darwin", {"XDG_STATE_HOME": ""}) == str(home / ".local" / "state" / "quota-router")
    assert pc.default_state_dir("windows", {"LOCALAPPDATA": "C:\\Users\\me\\AppData\\Local"}) == os.path.join(
        "C:\\Users\\me\\AppData\\Local", "quota-router")
    assert pc.default_state_dir("windows", {}) == os.path.join(str(home), "AppData", "Local", "quota-router")
    # XDG_STATE_HOME is a Unix convention; Windows ignores it.
    assert pc.default_state_dir("windows", {"XDG_STATE_HOME": "/x", "LOCALAPPDATA": "L"}) == os.path.join("L", "quota-router")


def test_config_dir_defaults_per_platform(home):
    assert pc.default_config_dir("linux", {}) == str(home / ".config" / "quota-router")
    assert pc.default_config_dir("darwin", {}) == str(home / ".config" / "quota-router")
    assert pc.default_config_dir("windows", {"APPDATA": "C:\\Users\\me\\AppData\\Roaming"}) == os.path.join(
        "C:\\Users\\me\\AppData\\Roaming", "quota-router")
    assert pc.default_config_dir("windows", {}) == os.path.join(str(home), "AppData", "Roaming", "quota-router")


def test_defaults_follow_the_current_environment(home, monkeypatch):
    monkeypatch.setattr(pc, "system", lambda: "linux")
    assert pc.default_state_dir() == str(home / ".local" / "state" / "quota-router")
    monkeypatch.setenv("XDG_STATE_HOME", str(home / "s"))
    assert pc.default_state_dir() == str(home / "s" / "quota-router")
    monkeypatch.setattr(pc, "system", lambda: "windows")
    monkeypatch.setenv("LOCALAPPDATA", str(home / "Local"))
    monkeypatch.setenv("APPDATA", str(home / "Roaming"))
    assert pc.default_state_dir() == str(home / "Local" / "quota-router")
    assert pc.default_config_dir() == str(home / "Roaming" / "quota-router")


def test_lock_is_exclusive_between_handles(tmp_path):
    path = tmp_path / "x.lock"
    with open(path, "a") as first, open(path, "a") as second:
        assert pc.try_lock(first) is True
        assert pc.try_lock(second) is False
        pc.unlock(first)
        assert pc.try_lock(second) is True
        pc.unlock(second)


class Late(Exception):
    pass


def test_deadline_abandons_a_call_that_overruns():
    started = time.monotonic()
    with pytest.raises(Late):
        pc.call_with_deadline(lambda: time.sleep(2), 0.05, Late)
    assert time.monotonic() - started < 1


def test_deadline_passes_results_and_errors_through():
    assert pc.call_with_deadline(lambda: 41 + 1, 5, Late) == 42
    with pytest.raises(KeyError, match="original"):
        pc.call_with_deadline(lambda: {}["original"], 5, Late)
    with pytest.raises(Late):                      # no time left at all: the call is not waited for
        pc.call_with_deadline(lambda: time.sleep(0.2), 0, Late)


def test_deadline_needs_no_main_thread_and_no_signal():
    import threading
    caller = threading.current_thread()
    # no deadline: nothing is started, the function runs right here.
    assert pc.call_with_deadline(threading.current_thread, float("inf"), Late) is caller
    assert pc.call_with_deadline(threading.current_thread, 5, Late) is not caller
    results = []
    thread = threading.Thread(target=lambda: results.append(pc.call_with_deadline(lambda: "from a worker", 5, Late)))
    thread.start()
    thread.join(5)
    assert results == ["from a worker"]             # SIGALRM could only ever be used from the main thread
    import signal
    if hasattr(signal, "getitimer"):
        assert signal.getitimer(signal.ITIMER_REAL)[0] == 0
    assert not hasattr(pc, "signal")


def test_deadline_keeps_the_callers_time_budget_visible():
    from quota_router import core
    with core.budget(time.monotonic() + 30):
        inside = pc.call_with_deadline(core.remaining, 5, Late)
    assert 25 < inside <= 30 and core.remaining() == float("inf")


def test_network_json_is_bounded_by_the_budget(router, monkeypatch):
    """A body that trickles in forever: every read succeeds, so no socket timeout ever fires."""
    import io
    core = router.core

    class Drip(io.BytesIO):
        def read(self, *args):
            time.sleep(3)
            return super().read(*args)
    seen = []
    monkeypatch.setattr(core, "open_url", lambda request, timeout: (seen.append(timeout), Drip(b"{}"))[1])
    started = time.monotonic()
    with core.budget(time.monotonic() + 1.2):
        with pytest.raises(core.BudgetTimeout):
            core.network_json("request")
    assert time.monotonic() - started < 2 and seen and seen[0] <= 1.2
    monkeypatch.setattr(core, "open_url", lambda request, timeout: io.BytesIO(b'{"ok": true}'))
    with core.budget(time.monotonic() + 5):
        assert core.network_json("request") == {"ok": True}
    assert core.network_json("request") == {"ok": True}     # no budget at all (qr status): still works


def fake_runner(outputs):
    calls = []

    def run(argv):
        calls.append(argv)
        value = outputs.get(argv[0] + " " + argv[1])
        return None if value is None else subprocess.CompletedProcess(argv, value[0], stdout=value[1], stderr="")
    run.calls = calls
    return run


def test_list_processes_and_environment_parse_ps_output(monkeypatch):
    monkeypatch.setattr(pc, "system", lambda: "linux")
    run = fake_runner({"ps -axww": (0, "  101     1 /bin/claude -p hi\n  202   101 codex exec x\nnoise\n"),
                       "ps eww": (0, "  /bin/claude -p hi CLAUDE_CONFIG_DIR=/a/b PATH=/bin\n")})
    assert pc.list_processes(run) == {101: (1, "/bin/claude -p hi"), 202: (101, "codex exec x")}
    assert pc.process_environment(101, "/bin/claude -p hi", run) == " CLAUDE_CONFIG_DIR=/a/b PATH=/bin\n"
    assert pc.process_environment(101, "something else", run) is None      # the process changed in between
    assert run.calls[0] == ["ps", "-axww", "-o", "pid=,ppid=,command="]
    assert run.calls[1] == ["ps", "eww", "-o", "command=", "-p", "101"]
    assert pc.list_processes(fake_runner({})) == {}                           # ps unavailable or out of time
    assert pc.process_environment(101, "x", fake_runner({})) is None


def test_claude_credentials_from_the_macos_keychain(home, monkeypatch):
    monkeypatch.setattr(pc, "system", lambda: "darwin")
    run = fake_runner({"security find-generic-password": (0, '{"claudeAiOauth": {}}')})
    assert pc.claude_credentials(None, run) == '{"claudeAiOauth": {}}'
    assert run.calls[-1] == ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"]
    pc.claude_credentials("/home/someone/.claude-b", run)
    service = run.calls[-1][3]
    assert service.startswith("Claude Code-credentials-") and len(service) == len("Claude Code-credentials-") + 8
    assert service == pc.keychain_service("/home/someone/.claude-b") != pc.keychain_service("/home/someone/.claude-c")
    assert pc.claude_credentials(None, fake_runner({"security find-generic-password": (44, "")})) is None
    assert pc.claude_credentials(None, fake_runner({})) is None


def test_macos_falls_back_to_the_credentials_file(home, monkeypatch):
    """Claude Code writes the file itself when the keychain refuses (locked, SSH session)."""
    monkeypatch.setattr(pc, "system", lambda: "darwin")
    (home / ".claude").mkdir()
    (home / ".claude" / ".credentials.json").write_text('{"claudeAiOauth": {"accessToken": "from-file"}}')
    (home / "b").mkdir()
    (home / "b" / ".credentials.json").write_text('{"claudeAiOauth": {"accessToken": "b-file"}}')
    for runner in (fake_runner({"security find-generic-password": (44, "")}), fake_runner({}),
                   fake_runner({"security find-generic-password": (0, "\n")})):
        assert "from-file" in pc.claude_credentials(None, runner)
        assert "b-file" in pc.claude_credentials(str(home / "b"), runner)
    # the keychain still wins when it has an entry.
    assert pc.claude_credentials(None, fake_runner({"security find-generic-password": (0, '{"k": 1}')})) == '{"k": 1}'
    (home / "b" / ".credentials.json").write_bytes(b"\xff\xfe")
    assert pc.claude_credentials(str(home / "b"), fake_runner({})) is None


def test_claude_credentials_from_the_credentials_file(home, monkeypatch):
    def no_commands(argv):
        raise AssertionError("no command should run on Linux")
    monkeypatch.setattr(pc, "system", lambda: "linux")
    assert pc.claude_credentials(None, no_commands) is None
    (home / ".claude").mkdir()
    (home / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "default"}}))
    (home / "other").mkdir()
    (home / "other" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "other"}}))
    assert json.loads(pc.claude_credentials(None, no_commands))["claudeAiOauth"]["accessToken"] == "default"
    assert json.loads(pc.claude_credentials(str(home / "other"), no_commands))["claudeAiOauth"]["accessToken"] == "other"
    assert pc.claude_credentials(str(home / "missing"), no_commands) is None


def test_core_reads_the_token_through_platform_compat(home, monkeypatch):
    from quota_router import core
    monkeypatch.setattr(pc, "system", lambda: "linux")
    (home / "acct").mkdir()
    (home / "acct" / ".credentials.json").write_text(json.dumps(
        {"claudeAiOauth": {"accessToken": "fake-token", "expiresAt": 2_000_000_000_000}}))
    assert core.claude_token({"key": "a", "dir": str(home / "acct")}) == ("fake-token", 2_000_000_000.0)
    assert core.claude_token({"key": "b", "dir": str(home / "nothing")}) == (None, None)
    (home / "acct" / ".credentials.json").write_text("not json")
    assert core.claude_token({"key": "a", "dir": str(home / "acct")}) == (None, None)


# ---- Windows branches, exercised with a faked platform ---------------------------------
class FakeMsvcrt:
    """msvcrt.locking as documented: locks `nbytes` at the current file position; LK_NBLCK fails at once with EACCES."""
    LK_UNLCK, LK_LOCK, LK_NBLCK = 0, 1, 2

    def __init__(self):
        self.held = {}        # file path → fd holding byte 0
        self.calls = []

    def locking(self, fd, mode, nbytes):
        position = os.lseek(fd, 0, os.SEEK_CUR)
        self.calls.append((mode, nbytes, position))
        assert (nbytes, position) == (1, 0), "must lock exactly byte 0"
        key = os.fstat(fd).st_ino
        if mode == self.LK_UNLCK:
            if self.held.get(key) != fd:
                raise PermissionError(errno.EACCES, "Permission denied")
            del self.held[key]
        elif key in self.held:
            raise PermissionError(errno.EACCES, "Permission denied")
        else:
            self.held[key] = fd


@pytest.fixture
def windows(monkeypatch):
    fake = FakeMsvcrt()
    monkeypatch.setattr(pc, "system", lambda: "windows")
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setattr(pc, "_windows_processes", None)
    return fake


def test_windows_lock_uses_byte_zero_and_reports_contention(windows, tmp_path):
    path = tmp_path / "x.lock"
    with open(path, "a") as first, open(path, "a") as second:
        first.write("left over text")                 # the position is not 0 any more
        first.flush()
        assert pc.try_lock(first) is True
        assert pc.try_lock(second) is False
        pc.unlock(first)
        assert pc.try_lock(second) is True
        pc.unlock(second)
    assert [mode for mode, _, _ in windows.calls] == [windows.LK_NBLCK, windows.LK_NBLCK, windows.LK_UNLCK,
                                                      windows.LK_NBLCK, windows.LK_UNLCK]


def test_windows_lock_passes_other_errors_on(windows, tmp_path, monkeypatch):
    def broken(fd, mode, nbytes):
        raise OSError(errno.EBADF, "Bad file descriptor")
    monkeypatch.setattr(windows, "locking", broken)
    with open(tmp_path / "x.lock", "a") as f:
        with pytest.raises(OSError):
            pc.try_lock(f)


def test_state_lock_works_on_top_of_the_windows_lock(windows, router):
    core = router.core
    with core.state_lock("leases") as outer:
        with core.state_lock("leases", timeout=0.05) as inner:
            assert (outer, inner) == (True, False)       # held: the second caller carries on without it
    with core.state_lock("leases") as again:
        assert again is True
    assert " | lock-timeout | - | - | leases\n" in (router.home / "state" / "hook-errors.log").read_text()
    core.add_lease("claude-b")
    assert [lease["key"] for lease in core.active_leases()] == ["claude-b"]
    assert windows.held == {}


WINDOWS_PROCESSES = [
    {"ProcessId": 10, "ParentProcessId": 1, "CommandLine": 'C:\\Git\\usr\\bin\\env.exe CLAUDE_CONFIG_DIR=C:/Users/me/.claude-b claude -p hi'},
    {"ProcessId": 11, "ParentProcessId": 10, "CommandLine": '"C:\\Users\\me\\.local\\bin\\claude.exe" -p hi'},
    {"ProcessId": 20, "ParentProcessId": 1, "CommandLine": '"C:\\Program Files\\nodejs\\node.exe" C:\\Users\\me\\AppData\\Roaming\\npm\\node_modules\\@openai\\codex\\bin\\codex.js exec "do it"'},
    {"ProcessId": 30, "ParentProcessId": 1, "CommandLine": None},
    {"ProcessId": "x", "ParentProcessId": 1, "CommandLine": "claude -p broken"},
]


def test_windows_process_list_comes_from_powershell_json(windows):
    run = fake_runner({"powershell.exe -NoProfile": (0, json.dumps(WINDOWS_PROCESSES))})
    procs = pc.list_processes(run)
    assert sorted(procs) == [10, 11, 20]
    assert procs[11] == (10, '"C:\\Users\\me\\.local\\bin\\claude.exe" -p hi')
    argv = run.calls[0]
    assert argv[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
    assert "Get-CimInstance Win32_Process" in argv[4] and "ProcessId,ParentProcessId,CommandLine" in argv[4]
    assert "ConvertTo-Json" in argv[4] and "wmic" not in argv[4]
    # a second look within a few seconds does not start PowerShell again.
    assert pc.list_processes(run) == procs and len(run.calls) == 1


@pytest.mark.parametrize("output", [None, (1, ""), (0, ""), (0, "not json"), (0, "null"), (0, "[3, null]")])
def test_windows_process_list_failures_give_an_empty_table(windows, output):
    assert pc.list_processes(fake_runner({"powershell.exe -NoProfile": output})) == {}


def test_windows_single_process_is_an_object_not_a_list(windows):
    run = fake_runner({"powershell.exe -NoProfile": (0, json.dumps(WINDOWS_PROCESSES[1]))})
    assert list(pc.list_processes(run)) == [11]


def test_windows_cannot_read_another_process_environment(windows):
    def no_commands(argv):
        raise AssertionError("nothing to run")
    assert pc.process_environment(11, "claude.exe -p hi", no_commands) is None


def test_windows_account_comes_from_the_command_lines(windows):
    procs = pc.parse_windows_processes(json.dumps(WINDOWS_PROCESSES))
    assert pc.directory_from_command_lines(11, procs, "CLAUDE_CONFIG_DIR") == "C:/Users/me/.claude-b"   # from the env.exe parent
    assert pc.directory_from_command_lines(10, procs, "CLAUDE_CONFIG_DIR") == "C:/Users/me/.claude-b"
    assert pc.directory_from_command_lines(20, procs, "CODEX_HOME") is None
    quoted = {5: (4, "codex.cmd exec x"), 4: (4, 'bash -c "CODEX_HOME=\'C:/Users/Jo Doe/.codex-b\' codex exec x"')}
    assert pc.directory_from_command_lines(5, quoted, "CODEX_HOME") == "C:/Users/Jo Doe/.codex-b"      # and a parent loop ends
    assert pc.directory_from_command_lines(5, {5: (4, "echo XCODEX_HOME=/no")}, "CODEX_HOME") is None


@pytest.mark.parametrize("directory", ["C:/Users/O'Brien/.claude-b", "C:/Users/Jo Doe/.claude-b", "C:/Users/a'b' c\"d/.claude-b",
                                       "C:/Users/me/.claude-b", "C:/it's/$(id) & `x`/.claude-b"])
def test_windows_busy_count_reads_back_the_hooks_own_quoting(windows, directory):
    """review: the prefix the hook writes for a directory with an apostrophe is several quoted pieces back to back."""
    from quota_router import hook
    command = hook.dir_prefix("claude", directory.replace("/", "\\")) + "claude.exe -p hi"
    procs = {7: (1, command), 8: (7, "claude.exe -p hi")}
    assert pc.directory_from_command_lines(8, procs, "CLAUDE_CONFIG_DIR") == directory
    assert pc.directory_from_command_lines(8, {**procs, 7: (1, f"env.exe X=1 CLAUDE_CONFIG_DIR=C:/old {command}")}, "CLAUDE_CONFIG_DIR") == directory


def test_windows_account_is_not_read_from_the_prompt(windows):
    """What follows the program name is the prompt; a task that talks about CLAUDE_CONFIG_DIR= must not be
    counted against whatever directory its text happens to mention."""
    prompt = '"explain CLAUDE_CONFIG_DIR=C:/fake and CODEX_HOME=C:/fake"'
    procs = {1: (0, "bash.exe"),
             10: (1, f"env.exe CLAUDE_CONFIG_DIR=C:/old CLAUDE_CONFIG_DIR=C:/Users/me/.claude-b claude.exe -p {prompt}"),
             11: (10, f"claude.exe -p {prompt}"),
             12: (1, f"claude.exe -p {prompt}"),
             20: (1, f"bash.exe -c \"CODEX_HOME=C:/Users/me/.codex-b codex exec 'say CODEX_HOME=C:/fake'\""),
             21: (20, f"node.exe C:\\npm\\codex.js exec {prompt}"),
             30: (1, '"C:\\Program Files\\Git\\usr\\bin\\env.exe" "CODEX_HOME=C:/Users/Jo Doe/.codex-b" codex.cmd exec x')}
    assert pc.directory_from_command_lines(11, procs, "CLAUDE_CONFIG_DIR") == "C:/Users/me/.claude-b"
    assert pc.directory_from_command_lines(12, procs, "CLAUDE_CONFIG_DIR") is None           # only the prompt mentions it
    assert pc.directory_from_command_lines(21, procs, "CODEX_HOME") == "C:/Users/me/.codex-b"
    assert pc.directory_from_command_lines(30, procs, "CODEX_HOME") == "C:/Users/Jo Doe/.codex-b"   # one argument quoted as a whole


def test_home_in_a_command_is_read_with_forward_slashes_on_windows(windows, router, monkeypatch):
    """First CI run: `CLAUDE_CONFIG_DIR=${HOME}/.claude-b` was reported as outside the pool on Windows, because
    the home directory was pasted into shell text with its backslashes, which shell parsing then removed."""
    hook = router.hook
    monkeypatch.setattr(router.core, "HOME", "C:\\Users\\Jo\\AppData")
    for written in ("${HOME}/.claude-b", '"${HOME}/.claude-b"', '"$HOME"/.claude-b', '"$HOME""/.claude-b"'):
        command = f"CLAUDE_CONFIG_DIR={written} claude -p x"
        assert hook.policy_setting(command, command.index("claude -p"), "CLAUDE_CONFIG_DIR", home=True) == (
            "active", "C:/Users/Jo/AppData/.claude-b"), written
    monkeypatch.setattr(pc, "system", lambda: "darwin")
    command = "CLAUDE_CONFIG_DIR=${HOME}/x claude -p x"
    monkeypatch.setattr(router.core, "HOME", "/home/odd\\name")                 # elsewhere a backslash is just a character
    assert hook.policy_setting(command, command.index("claude -p"), "CLAUDE_CONFIG_DIR", home=True)[1] is not None


def test_windows_busy_count_finds_an_account_with_an_apostrophe(windows, router, monkeypatch):
    from quota_router import hook
    directory = "C:\\Users\\O'Brien\\.claude-b"
    router.core.CLAUDE_ACCOUNTS[1]["dir"] = directory                # (a drive path is not absolute for the config loader here)
    monkeypatch.setattr(pc, "canonical", lambda path: str(path).replace("\\", "/").lower())     # what Windows' normcase amounts to
    table = {7: (1, hook.dir_prefix("claude", directory) + "claude.exe -p hi"), 8: (7, "claude.exe -p hi")}
    monkeypatch.setattr(pc, "list_processes", lambda run: table)
    assert router.real_running_jobs() == {"claude-b": 1}


def test_only_windows_guesses_from_command_lines(monkeypatch):
    monkeypatch.setattr(pc, "system", lambda: "linux")
    assert pc.directory_from_command_lines(1, {1: (0, "env CODEX_HOME=/x codex exec y")}, "CODEX_HOME") is None


def test_windows_busy_count_only_sees_directories_on_command_lines(windows, router, monkeypatch):
    core = router.core
    b, codex_b = str(router.home / ".claude-b"), str(router.home / ".codex-b")
    table = {10: (1, f"env.exe CLAUDE_CONFIG_DIR={b} claude -p hi"), 11: (10, "claude.exe -p hi"),
             12: (1, "claude.exe -p exported-earlier"),                      # account unknown: not counted anywhere
             20: (1, f'bash.exe -c "CODEX_HOME={codex_b} codex exec x"'), 21: (20, "node.exe C:\\npm\\node_modules\\@openai\\codex\\bin\\codex.js exec x"),
             22: (1, "codex.cmd exec default-location")}
    monkeypatch.setattr(pc, "list_processes", lambda run: table)
    assert router.real_running_jobs() == {"claude-b": 1, "codex-2": 1}


def test_windows_spawn_is_detached_and_finds_cmd_shims(windows, monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(pc.subprocess, "Popen", lambda argv, **kwargs: started.append((argv, kwargs)))
    monkeypatch.setattr(pc.shutil, "which", lambda name, path=None: f"C:\\npm\\{name}.cmd" if path == "P" else None)
    pc.spawn_detached(["codex", "exec", "x"], {"PATH": "P"}, None, None)
    pc.spawn_detached(["codex", "exec", "x"], {"PATH": "elsewhere"}, None, None)
    (argv, kwargs), (unresolved, _) = started
    assert argv == ["C:\\npm\\codex.cmd", "exec", "x"] and unresolved[0] == "codex"
    assert kwargs["creationflags"] == 0x00000008 | 0x00000200          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    assert kwargs["stdin"] is subprocess.DEVNULL and "start_new_session" not in kwargs


def test_posix_spawn_starts_a_new_session(monkeypatch):
    started = []
    monkeypatch.setattr(pc, "system", lambda: "linux")
    monkeypatch.setattr(pc.subprocess, "Popen", lambda argv, **kwargs: started.append((argv, kwargs)))
    pc.spawn_detached(["codex", "exec"], {}, None, None)
    assert started[0][0] == ["codex", "exec"] and started[0][1]["start_new_session"] is True and "creationflags" not in started[0][1]


def test_windows_replace_retries_while_the_target_is_open(windows, monkeypatch, tmp_path):
    attempts = []

    def flaky(source, target):
        attempts.append(source)
        if len(attempts) < 3:
            raise PermissionError(13, "in use")
    monkeypatch.setattr(pc.os, "replace", flaky)
    pc.replace("a", "b")
    assert len(attempts) == 3
    monkeypatch.setattr(pc.os, "replace", lambda source, target: (_ for _ in ()).throw(PermissionError(13, "in use")))
    with pytest.raises(PermissionError):
        pc.replace("a", "b")
    monkeypatch.setattr(pc, "system", lambda: "linux")      # elsewhere a PermissionError is final at once
    del attempts[:]
    monkeypatch.setattr(pc.os, "replace", flaky)
    with pytest.raises(PermissionError):
        pc.replace("a", "b")
    assert len(attempts) == 1


def test_windows_paths_compare_across_spellings(windows):
    assert pc.canonical("/c/home/Me/.claude-b") == pc.canonical("c:/home/Me/.claude-b")       # Git Bash spelling
    assert pc.canonical("/c") == pc.canonical("c:/")
    assert pc.canonical("/cc/home") != pc.canonical("c:/c/home")


def test_windows_credentials_come_from_the_file(windows, home):
    (home / ".claude").mkdir()
    (home / ".claude" / ".credentials.json").write_text('{"claudeAiOauth": {"accessToken": "w"}}')
    assert "w" in pc.claude_credentials(None, None)                      # no command is ever run
    assert pc.default_config_dir().endswith("quota-router")


# ---- command lines and paths in core / hook ------------------------------------------
@pytest.mark.parametrize("command, family", [
    ("claude -p hi", "claude"),
    ("/usr/local/bin/claude --print hi", "claude"),
    ('"C:\\Users\\me\\.local\\bin\\claude.exe" -p "hi there"', "claude"),
    ("C:\\Users\\me\\AppData\\Roaming\\npm\\claude.cmd -p hi", "claude"),
    ("CLAUDE.EXE -p hi", "claude"),
    ('"C:\\Program Files\\nodejs\\node.exe" "C:\\Users\\me\\AppData\\Roaming\\npm\\node_modules\\@anthropic-ai\\claude-code\\cli.js" -p hi', "claude"),
    ("node /opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/cli.js -p hi", "claude"),
    ("node --no-warnings C:\\npm\\node_modules\\claude\\cli.js --print=hi", "claude"),
    ("codex exec x", "codex"),
    ("codex.cmd exec x", "codex"),
    ('"C:\\Program Files\\nodejs\\node.exe" C:\\npm\\node_modules\\@openai\\codex\\bin\\codex.js exec x', "codex"),
    ("C:\\tools\\codex.exe -m gpt exec x", "codex"),
    # not one-shot subagents
    ("claude", None),
    ('"C:\\Users\\me\\AppData\\Local\\AnthropicClaude\\claude.exe"', None),
    ("claude.exe --chrome-native-host", None),
    ("node C:\\npm\\node_modules\\@anthropic-ai\\claude-code\\cli.js", None),
    ("node C:\\npm\\node_modules\\@anthropic-ai\\claude-code\\cli.js --chrome-native-host -p", None),
    ("node C:\\work\\claude-notes\\server.js -p 8080", None),
    ("node -p 1+1", None),
    ("codex.exe app-server", None),
    ("codex.exe exec-server", None),
    ("C:\\Git\\usr\\bin\\env.exe CODEX_HOME=C:/x codex exec y", None),
    ("C:\\Git\\bin\\bash.exe -c \"claude -p hi\"", None),
    ("", None),
])
def test_classify_proc_knows_windows_spellings(command, family):
    from quota_router import core
    assert core.classify_proc(command) == family


@pytest.mark.parametrize("command, families", [
    ("claude.exe -p hi --model opus", ["claude"]),
    ("C:/Users/me/bin/codex.cmd exec x", ["codex"]),
    ("CLAUDE.EXE -p hi", ["claude"]),
    ("claude.exe mcp list", []),
    ("notclaude.exe -p hi", []),
    ("claude.txt -p hi", []),
    ("echo claude.exe -p hi", []),
])
def test_hook_recognises_exe_and_cmd_names(command, families):
    from quota_router import hook
    assert [d["fam"] for d in hook.find_dispatches(command)] == families


def test_dir_label_shortens_the_home_directory(router, monkeypatch):
    core = router.core
    assert core.dir_label(None) == "default location"
    assert core.dir_label(str(router.home / ".claude-b")) == "~/.claude-b"
    assert core.dir_label(str(router.home)) == "~"
    shown = lambda path: path.replace("\\", "/") if sys.platform == "win32" else path
    assert core.dir_label(str(router.home) + "-other/x") == shown(str(router.home) + "-other/x")      # only a whole path component
    assert core.dir_label("/srv/codex") == "/srv/codex"
    monkeypatch.setattr(pc, "system", lambda: "windows")
    monkeypatch.setattr(core, "HOME", "C:\\Users\\me")
    monkeypatch.setattr(core.os.path, "normcase", lambda path: path.lower().replace("/", "\\"))
    assert core.dir_label("c:\\Users\\ME\\.claude-b") == "~/.claude-b"
    assert core.dir_label("D:\\logins\\b") == "D:/logins/b"


def test_resume_lookup_takes_directory_names_literally(router, write_config):
    """A directory with glob characters in its name, and a session id that is one."""
    from quota_router import config
    odd = router.home / ("acct [1]" if sys.platform == "win32" else "acct [1]*")     # `*` cannot be in a Windows file name
    (odd / "projects" / "-tmp").mkdir(parents=True)
    (odd / "projects" / "-tmp" / "abc.jsonl").write_text("{}")
    (odd / "sessions" / "2026" / "10").mkdir(parents=True)
    (odd / "sessions" / "2026" / "10" / "rollout-xyz.jsonl").write_text("{}")
    router.core.configure(config.load(write_config(
        f"[[accounts]]\nfamily = \"claude\"\nkey = \"main\"\ndir = \"\"\n"
        f"[[accounts]]\nfamily = \"claude\"\nkey = \"odd\"\ndir = '''{odd}'''\n"
        f"[[accounts]]\nfamily = \"codex\"\nkey = \"cx\"\ndir = '''{odd}'''\n")))
    hook = router.hook
    assert hook.resume_account("claude", {"id": "abc"}, "/tmp") == "odd"
    assert hook.resume_account("claude", {"id": "*"}, "/tmp") is None
    assert hook.resume_account("claude", {"continue": True}, "/tmp") == "odd"
    assert hook.resume_account("codex", {"id": "xyz"}, "/tmp") == "cx"
    assert hook.resume_account("codex", {"id": "nope"}, "/tmp") is None
    assert hook.resume_account("codex", {"last": True}, "/tmp") == "cx"
    assert hook.slug("C:\\Users\\me\\my app") == "C--Users-me-my-app" and hook.slug("/Users/me/app") == "-Users-me-app"


def test_failure_count_notices_a_replaced_log_without_inodes(router):
    core = router.core
    log = router.home / "state" / "decisions.log"
    stamp = time.strftime("%m-%d %H:%M:%S")
    line = f"{stamp} | hook | lead=x | sess=x | ? | fail | pick=- | Boom | cmd#-\n"
    log.write_text(line * 2)
    assert core.recent_failures() == 2
    index = core.load("failure-index.json", {})
    assert "inode" not in index["decisions.log"] and index["decisions.log"]["head"]
    with open(log, "a") as f:
        f.write(line)
    assert core.recent_failures() == 3                      # appended: counted from the saved offset
    # replaced by a file of the same length that starts differently (a rotated log): counted from the top again.
    other = line.replace("| hook |", "| qr   |").replace("| fail |", "| auto |")
    assert len(other) == len(line)
    log.write_text(other * 2 + line)
    assert core.recent_failures() == 1
    # an index written by the previous version (inode, no head) is rebuilt, not trusted.
    index = core.load("failure-index.json", {})
    index["decisions.log"].pop("head")
    index["decisions.log"].update(inode=1, counts={str(time.time()): 40})
    core.save("failure-index.json", index)
    assert core.recent_failures() == 1
