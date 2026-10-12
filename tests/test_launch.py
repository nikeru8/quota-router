"""`qr launch` and the `claude` launcher: which claude is started, on which account, and what is printed."""
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import threading

import pytest

from conftest import CONFIG, WINDOWS, as_word, child_env, posix_only

LAUNCH_FALSE_B = CONFIG.replace('key = "claude-b"\n', 'key = "claude-b"\nlaunch = false\n')
CLAUDE_ONLY = CONFIG.split("[[accounts]]\nfamily = \"codex\"")[0]


def decisions(home):
    path = home / "state" / "decisions.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def leases(home):
    path = home / "state" / "leases.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def fake_claude(directory, name="claude", text="#!/bin/sh\nexit 0\n"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text)
    path.chmod(0o755)
    return path


# ---- which claude ------------------------------------------------------------------
def test_the_real_claude_is_found_past_the_launcher(home, monkeypatch):
    from quota_router import install, launch, platform_compat
    monkeypatch.setattr(platform_compat, "system", lambda: "linux")
    mine = platform_compat.default_launcher_dir()
    fake_claude(home / ".local" / "share" / "quota-router" / "bin", text=install.launcher_text())
    copy = fake_claude(home / "other-copy", text=install.launcher_text(python="/elsewhere/python"))   # another installation's launcher
    real = fake_claude(home / "npm" / "bin")
    later = fake_claude(home / "later")
    path = os.pathsep.join([mine, str(copy.parent), "", "relative/bin", str(real.parent), str(later.parent)])
    assert launch.real_claude(path) == str(real)
    assert launch.real_claude(os.pathsep.join([mine, str(copy.parent)])) is None
    (real.parent / "claude").chmod(0o644)                           # not executable: not a program
    if not WINDOWS:
        assert launch.real_claude(path) == str(later)
    assert launch.is_launcher(copy) and not launch.is_launcher(later) and not launch.is_launcher(home / "missing")


def test_windows_looks_for_every_pathext_suffix():
    from quota_router import platform_compat
    assert platform_compat.executable_names("claude", "windows", {"PATHEXT": ".COM;.EXE;.CMD"}) == ["claude.com", "claude.exe", "claude.cmd"]
    assert platform_compat.executable_names("claude", "linux", {}) == ["claude"]
    assert platform_compat.launcher_name("windows") == "claude.cmd" and platform_compat.launcher_name("darwin") == "claude"
    assert platform_compat.default_launcher_dir("windows", {"LOCALAPPDATA": "C:\\Users\\me\\AppData\\Local"}).endswith(
        os.path.join("Local", "quota-router", "bin"))


@posix_only
def test_the_launcher_directory_follows_xdg_data_home(home, monkeypatch):
    from quota_router import platform_compat
    assert platform_compat.default_launcher_dir() == str(home / ".local" / "share" / "quota-router" / "bin")
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    assert platform_compat.default_launcher_dir() == str(home / "data" / "quota-router" / "bin")


# ---- what kind of start -------------------------------------------------------------
@pytest.mark.parametrize("args", [
    ["--help"], ["-h"], ["--version"], ["-v"], ["--model", "opus", "--help"],
    ["mcp", "list"], ["config", "get", "x"], ["doctor"], ["update"], ["install", "stable"], ["auth", "login"],
    ["plugin", "install", "x"], ["setup-token"], ["migrate-installer"], ["agents"],
    ["--model", "opus", "mcp", "list"],            # an option's value is not the subcommand, the next word is
    ["some-future-subcommand"],                     # unknown words are never taken for a prompt
])
def test_what_passes_through_untouched(args):
    from quota_router import launch
    assert launch.parse(args) is None


@pytest.mark.parametrize("args, expected", [
    ([], {"resume": None, "model": None}),
    (["fix the failing test"], {"resume": None, "model": None}),
    (["--model", "opus", "Fix it"], {"resume": None, "model": "opus"}),
    (["--model=sonnet"], {"resume": None, "model": "sonnet"}),
    (["-p", "hello"], {"resume": None, "model": None}),            # after -p a word is the prompt
    (["--dangerously-skip-permissions"], {"resume": None, "model": None}),
    (["--", "--help"], {"resume": None, "model": None}),           # after -- everything is the prompt
    (["--resume", "abc-123"], {"resume": {"id": "abc-123"}, "model": None}),
    (["-r", "abc-123", "--model", "opus"], {"resume": {"id": "abc-123"}, "model": "opus"}),
    (["--resume=abc-123"], {"resume": {"id": "abc-123"}, "model": None}),
    (["--continue"], {"resume": {"continue": True}, "model": None}),
    (["-c"], {"resume": {"continue": True}, "model": None}),
    (["--resume"], {"resume": {"continue": True}, "model": None}),       # the picker: where the latest conversation is
    (["-r", "--model", "opus"], {"resume": {"continue": True}, "model": "opus"}),
])
def test_sessions_are_recognised(args, expected):
    from quota_router import launch
    assert launch.parse(args) == expected


def test_an_account_already_chosen_is_left_alone(router, monkeypatch):
    from quota_router import launch
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(router.home / ".claude-b"))
    assert launch.choose(["fix it"]) == (launch.KEEP, None)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "")                      # set, even if empty, is a choice
    assert launch.choose(["fix it"]) == (launch.KEEP, None)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    # inside a Claude Code session the hook decides; its default account is `env -u CLAUDE_CONFIG_DIR claude -p …`.
    monkeypatch.setenv("CLAUDECODE", "1")
    assert launch.choose(["-p", "hi"]) == (launch.KEEP, None)
    monkeypatch.delenv("CLAUDECODE")
    assert launch.choose(["--version"]) == (launch.KEEP, None)
    assert launch.choose(["mcp", "list"]) == (launch.KEEP, None)
    assert decisions(router.home) == [] and leases(router.home) == []


def test_without_claude_accounts_nothing_is_managed(router, write_config):
    from quota_router import launch
    write_config(CONFIG.split("[[accounts]]", 3)[0] + "[[accounts]]" + CONFIG.split("[[accounts]]")[3])
    assert launch.choose(["fix it"]) == (launch.KEEP, None)


# ---- choosing --------------------------------------------------------------------
def test_a_new_session_goes_to_the_roomiest_account(router):
    from quota_router import launch
    directory, text = launch.choose(["--model", "opus", "fix it"])
    assert directory == str(router.home / ".claude-b")
    assert re.fullmatch(r"\[quota-router\] session on claude-b: weekly 80% left \(resets in [0-9.]+ h\)", text), text
    assert [lease["key"] for lease in leases(router.home)] == ["claude-b"]            # reserved like qr pick
    (logged,) = decisions(router.home)
    assert re.fullmatch(r"\d\d-\d\d \d\d:\d\d:\d\d \| qr launch \| lead=- \| sess=- \| claude \| launch \| pick=claude-b \| "
                        r"weekly 80% left; model other \| cmd#[0-9a-f]{12}", logged), logged
    assert "fix it" not in logged


def test_the_default_location_is_chosen_by_removing_the_variable(router):
    from quota_router import launch
    router.usage["claude-b"]["usage"]["seven_day"]["used"] = 70
    directory, text = launch.choose([])
    assert directory is None and text.startswith("[quota-router] session on claude-main: weekly 80% left")
    assert launch.environment(None).get("CLAUDE_CONFIG_DIR") is None
    assert launch.environment("/x")["CLAUDE_CONFIG_DIR"] == "/x"


def test_no_account_is_protected_as_the_lead(router):
    """The hook keeps the lead's account for the lead; a session being launched has no lead yet."""
    from quota_router import launch
    router.usage["claude-main"]["usage"]["seven_day"]["used"] = 85      # 15%: under protect_threshold (20), over weekly_floor (10)
    router.usage["claude-b"]["usage"]["seven_day"]["used"] = 100
    best, _, rows = router.core.choose("claude")                      # the hook's view: claude-main is the (assumed) lead
    assert best is None and "reserved for the lead" in router.core.find_row(rows, "claude-main")["blocked"]
    directory, text = launch.choose(["fix it"])
    assert directory is None and text.startswith("[quota-router] session on claude-main: weekly 15% left")


def test_accounts_with_launch_false_never_get_a_session(router, write_config):
    from quota_router import launch
    write_config(LAUNCH_FALSE_B)
    directory, text = launch.choose(["fix it"])                       # claude-b is roomier, but not for sessions
    assert directory is None and "claude-main" in text and "claude-b" not in text
    write_config(LAUNCH_FALSE_B.replace('key = "claude-main"\n', 'key = "claude-main"\nlaunch = false\n'))
    directory, text = launch.choose(["fix it"])
    assert (directory, text) == (None, "[quota-router] No Claude account in the config has launch enabled; starting on the default account.")
    assert "| launch-default | pick=claude-main | no account has launch enabled; model fable |" in decisions(router.home)[-1]


def test_one_login_in_two_directories_starts_where_launch_is_allowed(router, write_config):
    from quota_router import launch
    write_config(CONFIG.replace('key = "claude-main"\n', 'key = "claude-main"\nlaunch = false\n'))
    router.usage["claude-b"]["email"] = router.usage["claude-main"]["email"]       # the same account twice
    directory, _ = launch.choose(["fix it"])
    assert directory == str(router.home / ".claude-b")
    write_config(LAUNCH_FALSE_B)
    directory, _ = launch.choose(["fix it"])
    assert directory is None


def test_everything_blocked_starts_on_the_default_account(router):
    from quota_router import launch
    for key in ("claude-main", "claude-b"):
        router.usage[key]["usage"]["seven_day"]["used"] = 100
    directory, text = launch.choose(["fix it"])
    assert directory is None
    assert text == ("[quota-router] No account can take a new session (claude-main: weekly quota used up; "
                    "claude-b: weekly quota used up); starting on the default account.")
    assert leases(router.home) == []
    assert "| launch-default | pick=claude-main | no account can take it; model fable |" in decisions(router.home)[-1]


def test_unreadable_quota_starts_on_the_default_account(router):
    from quota_router import launch
    for key in ("claude-main", "claude-b"):
        router.usage[key] = {"email": key + "@example.test", "error": "HTTP 500", "fetched": 0}
    directory, text = launch.choose(["fix it"])
    assert (directory, text) == (None, "[quota-router] No account's quota could be read; starting on the default account.")
    assert leases(router.home) == [] and "no account could be read" in decisions(router.home)[-1]


def test_an_internal_error_starts_on_the_default_account(router, monkeypatch):
    from quota_router import launch
    monkeypatch.setattr(router.core, "read_family", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret detail")))
    directory, text = launch.choose(["fix it"])
    assert (directory, text) == (None, "[quota-router] Choosing an account failed (RuntimeError); starting on the default account.")
    assert "| qr launch | lead=- | sess=- | claude | fail | pick=- | RuntimeError | cmd#" in decisions(router.home)[-1]
    errors = (router.home / "state" / "hook-errors.log").read_text()
    assert "| launch | RuntimeError |" in errors and "secret detail" not in errors


def test_a_choice_that_hangs_is_abandoned(router, write_config, monkeypatch):
    from quota_router import launch
    write_config("[timing]\nhook_budget_seconds = 0\n" + CONFIG)
    monkeypatch.setattr(launch, "GRACE_SEC", 0.2)
    release = threading.Event()
    monkeypatch.setattr(launch, "decide", lambda *a: release.wait(10))
    try:
        assert launch.choose(["fix it"]) == (None, "[quota-router] Choosing an account took longer than 0 s; starting on the default account.")
    finally:
        release.set()


@pytest.mark.parametrize("text, line", [
    (None, '[quota-router] No config at {path} (run "qr init"); starting on the default account.'),
    ("[general\n", "[quota-router] Config error in {path}: not valid TOML"),
])
def test_a_missing_or_broken_config_starts_on_the_default_account(home, write_config, monkeypatch, text, line):
    from quota_router import launch
    path = home / "config.toml"
    if text is None:
        monkeypatch.setenv("QR_CONFIG", str(path))
    else:
        write_config(text)
    directory, said = launch.choose(["fix it"])
    assert directory is None and said.startswith(line.format(path=path))


def test_paid_overage_is_never_chosen_but_always_announced(router, monkeypatch):
    from quota_router import launch
    router.usage["claude-b"]["usage"]["seven_day"]["used"] = 100
    router.usage["claude-b"]["usage"]["extra_usage"] = {"is_enabled": True}
    directory, text = launch.choose(["fix it"])                       # an account that would bill is not picked
    assert directory is None and text.startswith("[quota-router] session on claude-main") and "⚠" not in text
    router.usage["claude-main"]["usage"]["five_hour"] = {"used": 95, "reset": router.usage["claude-b"]["usage"]["seven_day"]["reset"]}
    router.usage["claude-main"]["usage"]["seven_day"]["used"] = 100
    router.usage["claude-main"]["usage"]["extra_usage"] = {"is_enabled": True}
    directory, text = launch.choose(["fix it"])                       # nothing fits; the default account would bill
    assert directory is None and text.startswith("[quota-router] No account can take a new session (")
    assert text.endswith("⚠ PAID OVERAGE: claude-main is out of subscription quota and has paid overage enabled; this session will be billed.")
    assert text.count("\n") == 0
    monkeypatch.setenv("QR_ALLOW_CREDITS", "1")                       # allowed: chosen, and still said loudly
    directory, text = launch.choose(["fix it"])
    assert directory == str(router.home / ".claude-b") and "⚠ PAID OVERAGE: claude-b" in text
    assert "| launch | pick=claude-b | on paid overage;" in decisions(router.home)[-1] and "credits allowed" in decisions(router.home)[-1]


# ---- resumed conversations --------------------------------------------------------
def conversation(home, directory, cwd, session, mtime=None):
    from quota_router import hook
    folder = home / directory / "projects" / hook.slug(str(cwd))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{session}.jsonl"
    path.write_text("{}\n")
    if mtime:
        os.utime(path, (mtime, mtime))
    return path


@pytest.mark.parametrize("args", [["--resume", "abc-123"], ["-r", "abc-123"], ["--resume=abc-123"]])
def test_a_resumed_conversation_goes_where_it_lives(router, write_config, args):
    from quota_router import launch
    write_config(LAUNCH_FALSE_B)                     # launch = false does not apply: the conversation is only there
    router.usage["claude-b"]["usage"]["seven_day"]["used"] = 95
    conversation(router.home, ".claude-b", router.home / "work", "abc-123")
    directory, text = launch.choose(args, cwd=str(router.home / "work"))
    assert directory == str(router.home / ".claude-b")
    assert text.startswith("[quota-router] Resuming on the account that holds this conversation, claude-b: weekly 5% left")
    assert "⚠" not in text and [lease["key"] for lease in leases(router.home)] == ["claude-b"]
    assert "| launch-resume | pick=claude-b | kept the original account; weekly 5% left; model other |" in decisions(router.home)[-1]


def test_continue_goes_to_the_latest_conversation_in_this_directory(router):
    from quota_router import launch
    work = router.home / "work"
    conversation(router.home, ".claude", work, "old", mtime=1_000_000)
    conversation(router.home, ".claude-b", work, "new", mtime=2_000_000)
    conversation(router.home, ".claude", router.home / "elsewhere", "newest")
    assert launch.choose(["-c"], cwd=str(work))[0] == str(router.home / ".claude-b")
    os.utime(router.home / ".claude" / "projects" / router.hook.slug(str(work)) / "old.jsonl", (3_000_000, 3_000_000))
    assert launch.choose(["--continue"], cwd=str(work))[0] is None


def test_a_conversation_out_of_quota_still_resumes_with_a_warning(router):
    from quota_router import launch
    router.usage["claude-b"]["usage"]["seven_day"]["used"] = 100
    router.usage["claude-b"]["usage"]["extra_usage"] = {"is_enabled": True}
    conversation(router.home, ".claude-b", router.home, "abc")
    directory, text = launch.choose(["--resume", "abc"], cwd=str(router.home))
    assert directory == str(router.home / ".claude-b")
    assert "⚠ claude-b: weekly quota used up; the session starts there anyway" in text
    assert "⚠ PAID OVERAGE: claude-b" in text and text.count("\n") == 0


def test_an_unknown_conversation_starts_on_the_default_account(router):
    from quota_router import launch
    directory, text = launch.choose(["--resume", "nowhere"], cwd=str(router.home))
    assert (directory, text) == (None, "[quota-router] Could not find which account holds this conversation; starting on the default account.")
    assert "| launch-default | pick=claude-main | conversation location unknown;" in decisions(router.home)[-1]


def test_messages_in_traditional_chinese(router, monkeypatch):
    from quota_router import launch
    monkeypatch.setenv("QR_LANG", "zh-TW")
    directory, text = launch.choose(["fix it"])
    assert text.startswith("[帳號分流] 這個 session 開在 claude-b：週剩 80%")
    (router.home / "state" / "decisions.log").unlink()
    for key in ("claude-main", "claude-b"):
        router.usage[key]["usage"]["seven_day"]["used"] = 100
    assert launch.choose(["fix it"])[1].endswith("開在預設帳號。")
    assert "預設" not in "".join(decisions(router.home))                # the log stays English


# ---- starting claude ------------------------------------------------------------------
class Started(Exception):
    pass


@pytest.fixture
def exec_spy(monkeypatch):
    calls = []

    def execve(program, argv, env):
        calls.append((program, argv, env))
        raise Started()
    monkeypatch.setattr(os, "execve", execve)
    return calls


def test_main_execs_the_real_claude_with_the_account(router, exec_spy, monkeypatch, capsys):
    from quota_router import cli, platform_compat
    monkeypatch.setattr(platform_compat, "system", lambda: "linux")
    real = fake_claude(router.home / "npm-bin")
    monkeypatch.setenv("PATH", os.pathsep.join([platform_compat.default_launcher_dir(), str(real.parent)]))
    monkeypatch.setenv("KEEP_ME", "1")
    with pytest.raises(Started):
        cli.main(["launch", "--", "--model", "opus", "fix it"])
    ((program, argv, env),) = exec_spy
    assert program == str(real) and argv == [str(real), "--model", "opus", "fix it"]
    assert env["CLAUDE_CONFIG_DIR"] == str(router.home / ".claude-b") and env["KEEP_ME"] == "1"
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and err.startswith("[quota-router] session on claude-b")
    exec_spy.clear()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/chosen/by/you")
    with pytest.raises(Started):
        cli.main(["launch", "--", "--version"])
    assert exec_spy[0][1] == [str(real), "--version"] and exec_spy[0][2]["CLAUDE_CONFIG_DIR"] == "/chosen/by/you"
    assert capsys.readouterr().err == ""                              # pass-through prints nothing


def test_main_without_a_real_claude_exits_127(router, exec_spy, monkeypatch, capsys):
    from quota_router import cli, platform_compat
    monkeypatch.setenv("PATH", platform_compat.default_launcher_dir())
    with pytest.raises(SystemExit) as stopped:
        cli.main(["launch", "--", "fix it"])
    assert stopped.value.code == 127 and exec_spy == []
    assert capsys.readouterr().err.startswith("quota-router: no claude executable found on PATH apart from the quota-router launcher")


def test_windows_runs_claude_as_a_child_and_keeps_its_exit_status(router, monkeypatch):
    from quota_router import launch, platform_compat
    monkeypatch.setattr(platform_compat, "system", lambda: "windows")
    seen = []

    class Child:
        def __init__(self, argv, env):
            seen.append((argv, env))
            self.waits = 0

        def wait(self):
            self.waits += 1
            if self.waits == 1:
                raise KeyboardInterrupt()               # Ctrl+C belongs to claude; the launcher keeps waiting
            return 3
    monkeypatch.setattr(launch.subprocess, "Popen", Child)
    with pytest.raises(SystemExit) as stopped:
        launch.start("C:\\npm\\claude.cmd", ["-c"], None)
    assert stopped.value.code == 3 and seen[0][0] == ["C:\\npm\\claude.cmd", "-c"] and "CLAUDE_CONFIG_DIR" not in seen[0][1]


@posix_only
def test_the_installed_launcher_end_to_end(home, tmp_path, monkeypatch):
    """The file `qr install` writes, run by a real shell: claude --version passes through, a new session with no
    usable account starts on the default one with one line on stderr, a named account is kept, and the exit
    status of claude comes back. No network: nobody is logged in, and the endpoints point at a closed port."""
    from quota_router import cli, platform_compat
    stand_ins = tmp_path / "stand-ins"
    for name, script in (("security", "#!/bin/sh\nexit 44\n"), ("ps", "#!/bin/sh\nexit 0\n")):
        fake_claude(stand_ins, name, script)
    fake_claude(stand_ins, "claude", '#!/bin/sh\necho "dir=${CLAUDE_CONFIG_DIR-unset} args=$*"\nexit 7\n')
    config = tmp_path / "config.toml"
    config.write_text(CLAUDE_ONLY + '[endpoints]\nclaude_usage = "http://127.0.0.1:9/u"\nclaude_profile = "http://127.0.0.1:9/p"\n')
    monkeypatch.setenv("QR_CONFIG", str(config))
    cli.main(["install"])
    launcher = home / ".local" / "share" / "quota-router" / "bin" / "claude"
    assert launcher.exists() and os.access(launcher, os.X_OK)
    env = child_env(home, PATH=os.pathsep.join([str(launcher.parent), str(stand_ins), "/usr/bin", "/bin"]),
                    QR_CONFIG=str(config), QR_STATE=str(tmp_path / "state"), QR_NO_PROBE="1")

    def run(*args, **extra):
        return subprocess.run(["claude", *args], env=dict(env, **extra), capture_output=True, text=True, timeout=60, cwd=str(home))

    version = run("--version")
    assert (version.returncode, version.stdout, version.stderr) == (7, "dir=unset args=--version\n", "")
    session = run("fix", "it", CLAUDE_CONFIG_DIR="")
    assert session.stdout == "dir= args=fix it\n" and session.stderr == ""            # an account was named: left alone
    session = run("--model", "opus", "fix it")
    assert session.returncode == 7 and session.stdout == "dir=unset args=--model opus fix it\n"
    assert session.stderr.count("\n") == 1 and session.stderr.startswith("[quota-router] No account can take a new session (")
    assert "not logged in" in session.stderr and session.stderr.endswith("starting on the default account.\n")
    log = (tmp_path / "state" / "decisions.log").read_text()
    assert "| qr launch | lead=- | sess=- | claude | launch-default | pick=claude-main | no account can take it; model other |" in log
    assert "fix it" not in log


# ---- install / uninstall ---------------------------------------------------------------
def qr(*argv):
    from quota_router import cli
    cli.main(list(argv))


def launcher_file(home):
    from quota_router import install
    return install.launcher_path()


def test_install_writes_the_launcher_once(home, capsys):
    from quota_router import install, platform_compat
    qr("install")
    path = launcher_file(home)
    assert open(path, "rb").read() == install.launcher_text().encode()
    if not WINDOWS:
        assert path == str(home / ".local" / "share" / "quota-router" / "bin" / "claude")
        assert os.stat(path).st_mode & stat.S_IXUSR
        assert open(path).read().splitlines()[-1] == f"exec {shlex.join([sys.executable, '-I', '-X', 'utf8', install.ENTRY_FILE])} launch -- \"$@\""
    out = capsys.readouterr().out
    assert f"Wrote the claude launcher {path}" in out
    assert "comes first on PATH" in out and (platform_compat.default_launcher_dir() in out or "$HOME/.local/share/quota-router/bin" in out)
    record = json.load(open(install.record_path(), encoding="utf-8"))[os.path.normcase(path)]
    assert record["launcher"] is True and record["python"] == sys.executable and len(record["sha256"]) == 64
    before = os.stat(path).st_mtime_ns
    qr("install")
    assert os.stat(path).st_mtime_ns == before and "is already in place; nothing changed" in capsys.readouterr().out


@posix_only
def test_install_prints_the_path_line_only_when_needed(home, monkeypatch, capsys):
    from quota_router import install
    real = fake_claude(home / "npm-bin")
    mine = home / ".local" / "share" / "quota-router" / "bin"
    monkeypatch.setenv("PATH", str(real.parent))
    qr("install")
    out = capsys.readouterr().out
    assert '  export PATH="$HOME/.local/share/quota-router/bin:$PATH"' in out.splitlines()
    monkeypatch.setenv("PATH", os.pathsep.join([str(real.parent), str(mine)]))
    assert install.launcher_order() == ("late", str(real))
    qr("install")
    assert "export PATH=" in capsys.readouterr().out
    monkeypatch.setenv("PATH", os.pathsep.join([str(mine), str(real.parent)]))
    assert install.launcher_order() == ("ok", str(real))
    qr("install")
    assert "export PATH=" not in capsys.readouterr().out
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "my data"))
    assert install.path_line() == f"export PATH={shlex.quote(str(home / 'my data' / 'quota-router' / 'bin'))}:\"$PATH\""


def test_no_launcher_installs_the_hook_only(home, capsys):
    qr("install", "--no-launcher")
    assert not os.path.exists(launcher_file(home)) and "claude launcher" not in capsys.readouterr().out
    assert (home / ".claude" / "settings.json").exists()


def test_uninstall_removes_the_launcher_it_wrote_and_nothing_else(home, capsys):
    from quota_router import install
    qr("install")
    path = launcher_file(home)
    (home / "elsewhere").mkdir()
    qr("uninstall", "--no-launcher")
    assert os.path.exists(path)
    qr("uninstall")
    assert not os.path.exists(path) and os.path.isdir(os.path.dirname(path))      # the directory may be on PATH
    assert f"Removed the claude launcher {path}" in capsys.readouterr().out
    assert os.path.normcase(path) not in json.load(open(install.record_path(), encoding="utf-8"))
    qr("uninstall")
    assert "nothing removed" in capsys.readouterr().out


def test_a_launcher_that_is_not_ours_is_never_touched(home, capsys):
    from quota_router import install
    path = launcher_file(home)
    os.makedirs(os.path.dirname(path))
    with open(path, "w") as f:
        f.write("#!/bin/sh\n# my own wrapper\nexec /opt/claude \"$@\"\n")
    qr("install")
    assert "my own wrapper" in open(path).read()
    assert "already exists and was not written by this installation; left alone" in capsys.readouterr().out
    qr("uninstall")
    assert "my own wrapper" in open(path).read() and "left alone" in capsys.readouterr().out
    os.unlink(path)
    qr("install")
    with open(path, "a") as f:                                     # edited after install: no longer ours
        f.write("# edited\n")
    qr("uninstall")
    assert os.path.exists(path) and "was changed since; left alone" in capsys.readouterr().out
    assert install.launcher_is_ours(path, open(path, "rb").read()) is False


def test_a_launcher_recorded_for_another_interpreter_is_updated(home, capsys):
    from quota_router import install
    path = launcher_file(home)
    os.makedirs(os.path.dirname(path))
    old = install.launcher_text(python="/old/python3", entry="/old/quota_router/__main__.py").encode()
    with open(path, "wb") as f:
        f.write(old)
    install.remember(path, True, dict(install.launcher_record(old), python="/old/python3"))
    qr("install")
    assert open(path, "rb").read() == install.launcher_text().encode()
    assert f"Updated the claude launcher {path}" in capsys.readouterr().out


def test_the_windows_launcher_is_a_batch_file(monkeypatch):
    from quota_router import install
    text = install.launcher_text(python="C:\\Py\\python.exe", entry="C:\\site\\quota_router\\__main__.py", platform="windows")
    assert text.splitlines() == [
        "@echo off", "rem quota-router launcher: starts claude on the account with the most quota left (qr launch).",
        "rem Written by `qr install`; `qr uninstall` removes it.",
        '"C:\\Py\\python.exe" -I -X utf8 "C:\\site\\quota_router\\__main__.py" launch -- %*', "exit /b %ERRORLEVEL%"]
    assert "\r\n" in text


# ---- doctor -------------------------------------------------------------------------
@pytest.fixture
def machine(home, write_config, monkeypatch):
    """doctor with a fake process table and a PATH of fakes; the launcher installed."""
    from quota_router import install, platform_compat
    monkeypatch.setattr(platform_compat, "system", lambda: "linux")
    monkeypatch.setattr(install, "check_lock", lambda state: (True, ""))
    monkeypatch.setattr(platform_compat, "list_processes", lambda run: {1: (0, "init")})
    monkeypatch.setenv("QR_STATE", str(home / "state"))
    real = fake_claude(home / "npm-bin")
    monkeypatch.setenv("PATH", os.pathsep.join([platform_compat.default_launcher_dir(), str(real.parent)]))
    write_config()
    qr("install")
    return real


def doctor(capsys):
    capsys.readouterr()
    qr("doctor")
    lines = capsys.readouterr().out.splitlines()
    assert all(line.startswith(("OK    ", "WARN  ")) for line in lines)
    return lines


@posix_only
def test_doctor_checks_the_launcher(home, machine, monkeypatch, capsys):
    from quota_router import platform_compat
    path = launcher_file(home)
    mine = platform_compat.default_launcher_dir()
    lines = doctor(capsys)
    assert f"OK    claude CLI found at {machine}" in lines                     # the real one, not the launcher
    assert f"OK    claude launcher at {path}" in lines
    assert f"OK    {mine} comes before the real claude ({machine}) on PATH" in lines
    assert any(line.startswith("OK    no shell alias or function named claude in ") for line in lines)
    assert "WARN  no reading on file yet for the Claude accounts for new sessions (claude-main, claude-b); run `qr status`" in lines
    (home / "state" / "cache.json").write_text(json.dumps({"claude-b": {"usage": {"seven_day": {"used": 1}}}}))
    assert "OK    Claude accounts for new sessions: claude-main, claude-b; a reading is on file for claude-b" in doctor(capsys)
    os.chmod(path, 0o644)
    assert f"WARN  claude launcher {path} is not executable; run `qr install`" in doctor(capsys)
    monkeypatch.setenv("PATH", os.pathsep.join([str(machine.parent), mine]))
    assert any(line.startswith(f"WARN  {mine} comes after the real claude ({machine}) on PATH") and
               line.endswith('export PATH="$HOME/.local/share/quota-router/bin:$PATH"') for line in doctor(capsys))
    monkeypatch.setenv("PATH", str(machine.parent))
    assert any(line.startswith(f"WARN  {mine} is not on PATH") for line in doctor(capsys))


@posix_only
@pytest.mark.parametrize("text, expected", [
    ("alias claude='claude --dangerously-skip-permissions'",
     "OK    {file}:2: this alias still finds claude on PATH, so it goes through the launcher: alias claude='claude --dangerously-skip-permissions'"),
    ('alias claude="command claude --verbose"', "OK    {file}:2: this alias still finds claude on PATH"),
    ("alias claude=/opt/homebrew/bin/claude",
     "WARN  {file}:2: this alias runs /opt/homebrew/bin/claude instead of claude from PATH, so the launcher is bypassed: alias claude=/opt/homebrew/bin/claude"),
    ("alias claude='~/.npm/bin/claude --x'", "WARN  {file}:2: this alias runs ~/.npm/bin/claude instead of claude from PATH"),
    ("alias claude='CLAUDE_CONFIG_DIR=~/.claude-b claude'", "WARN  {file}:2: this alias sets CLAUDE_CONFIG_DIR"),
    ("claude() { command claude \"$@\"; }", "WARN  {file}:2: a shell function named claude decides what runs"),
])
def test_doctor_explains_a_claude_alias(home, machine, capsys, text, expected):
    rc = home / ".zshrc"
    rc.write_text("export EDITOR=vim\n" + text + "\n")
    lines = doctor(capsys)
    assert any(line.startswith(expected.format(file=rc)) for line in lines), lines
    assert not any(line.startswith("OK    no shell alias") for line in lines)


def test_doctor_masks_secrets_in_an_alias_line(home, machine, capsys):
    (home / ".bashrc").write_text("alias claude='ANTHROPIC_API_KEY=sk-ant-abcdefghijklmnopqrstuvwxyz0123 claude'\n")
    text = "\n".join(doctor(capsys))
    assert ".bashrc:1:" in text and "sk-ant-abcdefghij" not in text


def test_doctor_without_the_launcher_or_launch_accounts(home, machine, write_config, capsys):
    os.unlink(launcher_file(home))
    lines = doctor(capsys)
    assert f"WARN  no claude launcher at {launcher_file(home)}, so new sessions start on whatever account `claude` uses; run `qr install` to add it (optional)" in lines
    assert not any("PATH" in line and "comes" in line for line in lines)
    qr("install")
    write_config(LAUNCH_FALSE_B.replace('key = "claude-main"\n', 'key = "claude-main"\nlaunch = false\n'))
    assert "WARN  no Claude account has launch enabled, so new sessions always start on the default account" in doctor(capsys)


# ---- config ---------------------------------------------------------------------------
def test_the_launch_setting(home, write_config, capsys):
    from quota_router import cli, config
    cfg = config.parse_text(LAUNCH_FALSE_B)
    assert [(a["key"], a["launch"]) for a in cfg.accounts] == [("claude-main", True), ("claude-b", False), ("codex-1", False), ("codex-2", False)]
    assert cfg.warnings == []
    with pytest.raises(config.ConfigError, match='account "claude-b": launch must be true or false'):
        config.parse_text(CONFIG.replace('key = "claude-b"\n', 'key = "claude-b"\nlaunch = "no"\n'))
    codex = config.parse_text(CONFIG.replace('key = "codex-2"\n', 'key = "codex-2"\nlaunch = true\n'))
    assert codex.warnings == ['"launch" in account "codex-2" only applies to Claude accounts (ignored)']
    write_config(LAUNCH_FALSE_B)
    cli.main(["config"])
    out = capsys.readouterr().out
    assert f"claude  claude-b        CLAUDE_CONFIG_DIR={home / '.claude-b'}  (launch = false: no new sessions)  [config file]" in out.splitlines()


def test_launch_false_does_not_change_the_hook(router, write_config):
    """launch = false is about interactive sessions only; dispatches still go there."""
    write_config(LAUNCH_FALSE_B)
    router.core.configure(__import__("quota_router.config", fromlist=["load"]).load())
    result = router.hook.decide("claude -p hi --model opus", str(router.home))
    assert result["command"] == f"env CLAUDE_CONFIG_DIR={as_word(router.home / '.claude-b')} claude -p hi --model opus"


def test_help_lists_launch(home, capsys):
    from quota_router import cli
    cli.main(["--help"])
    out = capsys.readouterr().out
    assert "qr launch [--] [ARGS…]" in out and "--no-launcher" in out
