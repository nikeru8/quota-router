"""`qr install`, `qr uninstall`, `qr doctor`. Everything happens inside the throwaway home directory."""
import json
import os
import shlex
import subprocess
import sys

import pytest

from conftest import CONFIG, ROOT, child_env, default_config_file

EXISTING = """{
    "model": "opus",
    "permissions": {"allow": ["Bash(ls:*)"]},
    "hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo 中文 >> /tmp/log"}]},
            {"matcher": "Write", "hooks": [{"type": "command", "command": "lint", "args": ["--fix"]}]}
        ],
        "Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]
    }
}
"""


def qr(*argv):
    from quota_router import cli
    cli.main(list(argv))


def settings(home, name=".claude"):
    return home / name / "settings.json"


def backups(path):
    return sorted(p for p in path.parent.iterdir() if p.name.startswith("settings.json.bak-"))


def our_hooks(path):
    from quota_router import install
    data = json.loads(path.read_text(encoding="utf-8"))
    return [hook for group in data.get("hooks", {}).get("PreToolUse", []) for hook in group["hooks"] if install.is_ours(hook)]


def test_install_into_a_fresh_directory(home, capsys):
    from quota_router import install
    qr("install")
    path = settings(home)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [install.hook_entry()]}]}}
    entry = data["hooks"]["PreToolUse"][0]["hooks"][0]
    # exec form: an absolute interpreter, no shell, isolated mode.
    assert entry["type"] == "command" and entry["command"] == sys.executable and os.path.isabs(entry["command"])
    assert entry["args"][0] == "-I" and entry["args"][-1] == "hook" and os.path.isabs(entry["args"][-2])
    assert os.path.samefile(entry["args"][-2], os.path.join(ROOT, "quota_router", "__main__.py"))
    assert backups(path) == []                               # there was nothing to back up
    out = capsys.readouterr().out
    assert f"Added the quota-router hook to {path}" in out and "qr doctor" in out


def test_install_twice_adds_one_entry(home, capsys):
    qr("install")
    path = settings(home)
    first = path.read_bytes()
    qr("install")
    qr("install")
    assert path.read_bytes() == first and len(our_hooks(path)) == 1 and backups(path) == []
    assert "already in" in capsys.readouterr().out


def test_install_keeps_everything_else_and_backs_up(home, capsys):
    path = settings(home)
    path.parent.mkdir()
    path.write_text(EXISTING, encoding="utf-8")
    qr("install")
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    before = json.loads(EXISTING)
    assert list(data) == list(before)                                             # key order
    assert data["model"] == "opus" and data["permissions"] == before["permissions"] and data["hooks"]["Stop"] == before["hooks"]["Stop"]
    assert data["hooks"]["PreToolUse"][:2] == before["hooks"]["PreToolUse"]       # existing hooks untouched, ours appended
    assert len(data["hooks"]["PreToolUse"]) == 3 and data["hooks"]["PreToolUse"][2]["matcher"] == "Bash"
    assert "echo 中文 >> /tmp/log" in text and '\n    "model": "opus"' in text and text.endswith("}\n")   # not escaped, same indent
    saved = backups(path)
    assert len(saved) == 1 and saved[0].read_text(encoding="utf-8") == EXISTING
    assert f"Backup of the previous file: {saved[0]}" in capsys.readouterr().out
    qr("install")
    assert len(backups(path)) == 1 and path.read_text(encoding="utf-8") == text


def test_uninstall_removes_only_its_own_entry(home, capsys):
    path = settings(home)
    path.parent.mkdir()
    path.write_text(EXISTING, encoding="utf-8")
    qr("install")
    qr("uninstall")
    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(EXISTING)
    assert len(backups(path)) == 2                                               # one per change
    qr("uninstall")
    assert len(backups(path)) == 2 and "nothing changed" in capsys.readouterr().out


def test_uninstall_leaves_no_empty_containers(home):
    qr("install")
    qr("uninstall")
    assert json.loads(settings(home).read_text()) == {}
    path = settings(home)
    path.write_text('{"hooks": {"PreToolUse": [], "Stop": []}, "x": 1}')
    qr("install")
    qr("uninstall")
    assert json.loads(path.read_text()) == {"hooks": {"Stop": []}, "x": 1}


def test_uninstall_without_a_settings_file_creates_nothing(home, capsys):
    qr("uninstall")
    assert not settings(home).exists() and not settings(home).parent.exists()


def test_install_updates_an_entry_it_recorded_for_another_interpreter(home, capsys):
    """An earlier `qr install` from another interpreter and checkout wrote down what it installed; that entry,
    and only that one, is brought up to date."""
    from quota_router import install
    path = settings(home)
    path.parent.mkdir()
    stale = {"type": "command", "command": "/old/python3", "args": ["-I", "/old/site-packages/quota_router/__main__.py", "hook"]}
    path.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [stale, {"type": "command", "command": "other"}]},
                                                         {"matcher": "Bash", "hooks": [install.hook_entry()]}]}}))
    os.makedirs(os.path.dirname(install.record_path()))
    with open(install.record_path(), "w", encoding="utf-8") as f:
        json.dump({os.path.normcase(str(path)): {"python": "/old/python3", "entry": "/old/site-packages/quota_router/__main__.py",
                                                 "args": stale["args"]}}, f)
    assert install.installed_state()[0] == "outdated"
    qr("install")
    groups = json.loads(path.read_text())["hooks"]["PreToolUse"]
    assert groups == [{"matcher": "Bash", "hooks": [install.hook_entry(), {"type": "command", "command": "other"}]}]
    assert "Updated the quota-router hook" in capsys.readouterr().out and install.installed_state()[0] == "installed"
    with open(install.record_path(), encoding="utf-8") as f:                 # and the record now names this interpreter
        assert json.load(f)[os.path.normcase(str(path))] == {"python": sys.executable, "entry": install.ENTRY_FILE,
                                                             "args": install.hook_entry()["args"]}


FOREIGN = [
    # review reproduction: a diagnostic hook that only mentions the path in its arguments
    {"type": "command", "command": "/usr/bin/printf", "args": ["%s\n", "/tmp/quota_router/__main__.py", "hook"]},
    # another installation: right shape, but neither this interpreter nor this package, and nothing recorded
    {"type": "command", "command": "/old/python3", "args": ["-I", "/old/site-packages/quota_router/__main__.py", "hook"]},
    {"type": "command", "command": "C:\\Py\\python.exe", "args": ["-I", "C:\\src\\quota_router\\__main__.py", "hook"]},
]


@pytest.mark.parametrize("entry", FOREIGN + [
    # half right is not enough: this interpreter with another package, this package run by something that is not Python.
    {"type": "command", "command": sys.executable, "args": ["-I", "/elsewhere/quota_router/__main__.py", "hook"]},
    {"type": "command", "command": "/other/wrapper", "args": ["-I", "-X", "utf8", "ENTRY", "hook"]},
    # final review reproduction: the entry file is only an argument of `python -c`, which runs something else.
    {"type": "command", "command": sys.executable, "args": ["-c", "print(1)", "ENTRY", "hook"]},
    {"type": "command", "command": sys.executable, "args": ["-I", "-X", "utf8", "ENTRY", "extra", "hook"]}])
def test_entries_that_are_not_ours_are_never_touched(home, capsys, entry):
    from quota_router import install
    entry = json.loads(json.dumps(entry).replace("ENTRY", install.ENTRY_FILE.replace("\\", "\\\\")))
    path = settings(home)
    path.parent.mkdir()
    text = json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [entry]}]}})
    path.write_text(text)
    assert not install.is_ours(entry) and install.is_foreign(entry)
    assert install.installed_state() == ("foreign", entry)
    qr("uninstall")
    qr("install")                                             # a second hook would route every command twice
    assert path.read_text() == text and backups(path) == []
    out = capsys.readouterr().out
    assert "nothing changed" in out and "did not put there" in out


def test_a_marker_alone_does_not_make_an_entry_ours():
    from quota_router import install
    marked = {"type": "command", "command": "/usr/bin/printf", "args": ["x", "hook"], "_quota_router": True}
    assert not install.is_ours(marked) and not install.is_foreign(marked)
    assert install.is_ours(install.hook_entry())
    assert not install.is_ours(dict(install.hook_entry(), command="/usr/bin/printf"))
    assert not install.is_ours(dict(install.hook_entry(), args=["%s", "/tmp/quota_router/__main__.py", "hook"]))


def test_uninstall_keeps_groups_that_were_already_empty(home):
    """review reproduction: removing our entry must not sweep away somebody else's empty matcher group."""
    path = settings(home)
    path.parent.mkdir()
    before = {"hooks": {"PreToolUse": [{"matcher": "Write", "hooks": []}, {"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]}]}}
    path.write_text(json.dumps(before))
    qr("install")
    assert len(json.loads(path.read_text())["hooks"]["PreToolUse"]) == 3
    qr("uninstall")
    assert json.loads(path.read_text()) == before
    # ours inside somebody else's group: the group stays, with what else was in it.
    from quota_router import install
    shared = {"hooks": {"PreToolUse": [{"matcher": "Write", "hooks": []}, {"matcher": "Bash", "hooks": [install.hook_entry()]}]}}
    path.write_text(json.dumps(shared))
    qr("uninstall")
    assert json.loads(path.read_text()) == {"hooks": {"PreToolUse": [{"matcher": "Write", "hooks": []}]}}


@pytest.mark.parametrize("matcher, reached", [("Bash", True), ("*", True), ("", True), (None, True), ("Bash|Write", True),
                                              ("Write", False), ("Edit|Write", False), ("Bash(", False), (7, False)])
def test_the_hook_must_sit_under_a_matcher_that_covers_bash(home, capsys, matcher, reached):
    """review reproduction: the exact entry under matcher "Write" used to count as installed."""
    from quota_router import install
    path = settings(home)
    path.parent.mkdir()
    group = {"hooks": [install.hook_entry(), {"type": "command", "command": "other"}]}
    if matcher is not None:
        group["matcher"] = matcher
    path.write_text(json.dumps({"hooks": {"PreToolUse": [group]}}))
    assert install.installed_state()[0] == ("installed" if reached else "misplaced")
    qr("install")
    groups = json.loads(path.read_text())["hooks"]["PreToolUse"]
    if reached:
        assert groups == [group] and "nothing changed" in capsys.readouterr().out
    else:
        # moved: the other hook stays where it was, ours now runs for Bash.
        assert groups == [dict(group, hooks=[{"type": "command", "command": "other"}]), {"matcher": "Bash", "hooks": [install.hook_entry()]}]
        assert "Updated the quota-router hook" in capsys.readouterr().out
    assert install.installed_state() == ("installed", install.hook_entry())


def test_a_command_that_only_mentions_qr_hook_is_not_a_hook(home, capsys):
    """review reproduction: `echo qr hook` counted as a hand-written hook, so nothing was installed."""
    from quota_router import install
    path = settings(home)
    path.parent.mkdir()
    path.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo qr hook"}]},
                                                         {"matcher": "Write", "hooks": [{"type": "command", "command": "/x/bin/qr hook"}]}]}}))
    assert install.installed_state() == ("absent", None)       # the real one is under a matcher Bash never reaches
    qr("install")
    assert install.installed_state()[0] == "installed" and "Added the quota-router hook" in capsys.readouterr().out


def test_install_leaves_a_hand_written_entry_alone(home, capsys):
    from quota_router import install
    path = settings(home)
    path.parent.mkdir()
    text = json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "/opt/quota-router/bin/qr hook"}]}]}})
    path.write_text(text)
    qr("install")
    qr("uninstall")
    assert path.read_text() == text and backups(path) == []
    out = capsys.readouterr().out
    assert "hand-written entry" in out and install.installed_state()[0] == "manual"
    for command in ("qr hook", '"C:/Program Files/qr/qr.exe" hook', "/x/bin/qr  hook ", r"C:\Tools\qr.exe hook",
                    "python3 /x/bin/qr hook", "QR_LANG=en /usr/bin/python3.13 -X utf8 -I /x/bin/qr hook"):
        assert install.is_manual({"type": "command", "command": command}), command
    for command in ("myqr hook", "qr hooks", "echo qr hook done", "qr status", "echo qr hook", "true; qr hook", "qr hook; rm x",
                    "echo 'qr hook", "python3 -c pass qr hook"):
        assert not install.is_manual({"type": "command", "command": command}), command


@pytest.mark.parametrize("text, detail", [("{not json", "not valid JSON"), ("[1, 2]", "not a JSON object"),
                                          ('{"hooks": []}', '"hooks" is not in the shape'),
                                          ('{"hooks": {"PreToolUse": {"Bash": 1}}}', '"hooks.PreToolUse" is not in the shape')])
def test_unreadable_settings_are_never_overwritten(home, capsys, text, detail):
    from quota_router import install
    path = settings(home)
    path.parent.mkdir()
    path.write_text(text)
    for command in ("install", "uninstall"):
        with pytest.raises(SystemExit) as stop:
            qr(command)
        err = capsys.readouterr().err
        assert stop.value.code == 2 and detail in err and "nothing changed" in err and str(path) in err
    assert path.read_text() == text and backups(path) == []
    assert install.installed_state() == ("unreadable", None)


def test_config_dir_option(home, capsys):
    other = home / "Team Account"
    qr("install", "--config-dir", str(other))
    qr("install", f"--config-dir={other}")
    assert len(our_hooks(other / "settings.json")) == 1 and not settings(home).exists()
    qr("install", "--config-dir", "~/.claude-b")
    assert len(our_hooks(home / ".claude-b" / "settings.json")) == 1
    qr("uninstall", "--config-dir", str(other))
    assert our_hooks(other / "settings.json") == [] and len(our_hooks(home / ".claude-b" / "settings.json")) == 1
    with pytest.raises(SystemExit) as stop:
        qr("install", "--config-dir")
    assert stop.value.code == 2 and "usage: qr install|uninstall [--config-dir DIR]" in capsys.readouterr().err


def test_messages_follow_the_language(home, write_config, monkeypatch, capsys):
    from quota_router import i18n
    monkeypatch.setenv("QR_LANG", "zh-TW")
    qr("install")
    assert "已把 quota-router 的 hook 加進" in capsys.readouterr().out
    monkeypatch.delenv("QR_LANG")
    write_config('[general]\nlanguage = "zh-TW"\n' + CONFIG)
    qr("uninstall")
    assert "已從" in capsys.readouterr().out
    i18n.set_language("en")


def test_installed_hook_runs_end_to_end(home):
    """The exact command line that was written to settings.json, run the way Claude Code runs an exec-form hook."""
    qr("install")
    entry = json.loads(settings(home).read_text())["hooks"]["PreToolUse"][0]["hooks"][0]
    event = json.dumps({"tool_name": "Bash", "tool_input": {"command": "claude -p hi"}, "cwd": str(home)})
    env = child_env(home, PYTHONPATH=str(home / "nowhere"))      # -I: a stray PYTHONPATH must not matter
    result = subprocess.run([entry["command"], *entry["args"]], input=event.encode("utf-8"), env=env, cwd=str(home),
                            capture_output=True, timeout=60)
    assert (result.returncode, result.stdout) == (0, b"")                 # no config yet: fail open
    assert result.stderr.decode("utf-8").startswith("quota-router: no config at ")


def test_python_m_entry_point(home):
    env = child_env(home, PYTHONPATH=ROOT)
    result = subprocess.run([sys.executable, "-m", "quota_router", "--help"], env=env, capture_output=True, text=True,
                            encoding="utf-8", timeout=60)
    assert result.returncode == 0 and "qr install [--config-dir DIR]" in result.stdout and "qr doctor" in result.stdout


# ---- doctor ---------------------------------------------------------------------------
@pytest.fixture
def quiet_machine(home, monkeypatch):
    """doctor looks at the process table and PATH; give it fakes so the test neither depends on nor reads the real ones."""
    from quota_router import install, platform_compat
    monkeypatch.setenv("PATH", str(home / "empty-bin"))
    monkeypatch.setattr(platform_compat, "list_processes", lambda run: {1: (0, "init"), 2: (1, "claude -p x")})
    monkeypatch.setattr(install.shutil, "which", lambda name, **kwargs: {"claude": "/fake/bin/claude"}.get(name))
    return platform_compat


def doctor_lines(capsys):
    qr("doctor")
    lines = capsys.readouterr().out.splitlines()
    assert lines and all(line.startswith(("OK    ", "WARN  ")) for line in lines)
    return lines


def test_doctor_on_an_empty_machine(home, quiet_machine, capsys):
    lines = doctor_lines(capsys)
    assert lines[0].startswith(f"OK    Python {sys.version_info[0]}.{sys.version_info[1]}.") and sys.executable in lines[0]
    assert any(line.startswith("WARN  no config file at ") and 'run "qr init"' in line for line in lines)
    assert "OK    file locking works" in lines and "OK    process list readable (2 processes seen)" in lines
    assert f"WARN  hook not installed in {settings(home)}; run `qr install`" in lines
    assert "OK    claude CLI found at /fake/bin/claude" in lines and "WARN  codex CLI not found on PATH" in lines
    assert not (home / ".claude").exists()                        # looking changes nothing


def test_doctor_reports_each_account(home, write_config, quiet_machine, monkeypatch, capsys):
    from quota_router import install
    monkeypatch.setattr(quiet_machine, "system", lambda: "linux")
    monkeypatch.setattr(install, "check_lock", lambda state: (True, ""))     # the real lock is checked in the other tests
    monkeypatch.setenv("QR_STATE", str(home / "state"))
    write_config()
    (home / ".claude").mkdir()
    (home / ".claude" / ".credentials.json").write_text("SECRET-CONTENT")
    (home / ".claude-b").mkdir()
    (home / ".codex-b").mkdir()
    (home / ".codex-b" / "auth.json").write_text("SECRET-CONTENT")
    qr("install")
    capsys.readouterr()
    lines = doctor_lines(capsys)
    text = "\n".join(lines)
    assert f"OK    config file {home / 'config.toml'}: 4 account(s)" in lines
    assert f"OK    claude claude-main: login file present at {home / '.claude' / '.credentials.json'}" in lines
    assert f"WARN  claude claude-b: no login file at {home / '.claude-b' / '.credentials.json'}; sign in with that directory first" in lines
    assert f"WARN  codex codex-1: directory {home / '.codex'} does not exist" in lines
    assert f"OK    codex codex-2: login file present at {home / '.codex-b' / 'auth.json'}" in lines
    assert f"OK    hook installed in {settings(home)}" in lines
    assert f"WARN  hook not installed in {settings(home, '.claude-b')}; run `qr install --config-dir {quiet_machine.shell_quote(str(home / '.claude-b'))}`" in lines
    assert "SECRET-CONTENT" not in text                           # existence only, never contents
    assert not (home / "state" / "doctor.lock").exists()


def test_doctor_on_macos_does_not_touch_the_keychain(home, write_config, quiet_machine, monkeypatch, capsys):
    from quota_router import core, install
    monkeypatch.setattr(quiet_machine, "system", lambda: "darwin")
    monkeypatch.setattr(install, "check_lock", lambda state: (True, ""))
    monkeypatch.setattr(core.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("doctor ran a command")))
    monkeypatch.setenv("QR_STATE", str(home / "state"))
    write_config()
    (home / ".claude-b").mkdir()
    lines = doctor_lines(capsys)
    assert f"OK    claude claude-b: {home / '.claude-b'} exists; on macOS the login is normally in the keychain (not checked)" in lines


def test_doctor_explains_the_windows_limits(home, quiet_machine, monkeypatch, capsys):
    import types
    from quota_router import install
    held = set()

    def locking(fd, mode, nbytes):
        if mode == 0:
            held.discard("x")
        elif "x" in held:
            raise PermissionError(13, "Permission denied")
        else:
            held.add("x")
    monkeypatch.setitem(sys.modules, "msvcrt", types.SimpleNamespace(LK_UNLCK=0, LK_NBLCK=2, locking=locking))
    monkeypatch.setattr(quiet_machine, "system", lambda: "windows")
    monkeypatch.setenv("LOCALAPPDATA", str(home / "Local"))
    monkeypatch.setenv("APPDATA", str(home / "Roaming"))
    lines = doctor_lines(capsys)
    assert "OK    platform: windows" in lines and "OK    file locking works" in lines
    assert any(line.startswith("WARN  Windows: another process's environment is not readable") for line in lines)
    assert any(line.startswith("WARN  bash not found on PATH; Claude Code needs Git for Windows") for line in lines)
    monkeypatch.setattr(install.shutil, "which", lambda name, **kwargs: "C:\\Git\\bin\\bash.exe" if name == "bash" else None)
    assert "OK    bash found at C:\\Git\\bin\\bash.exe (Git for Windows)" in doctor_lines(capsys)


def test_doctor_flags_broken_basics(home, write_config, quiet_machine, monkeypatch, capsys):
    from quota_router import install
    write_config("[general\n")
    monkeypatch.setattr(quiet_machine, "list_processes", lambda run: {})
    monkeypatch.setattr(quiet_machine, "try_lock", lambda file: True)        # a lock two handles can both take
    monkeypatch.setattr(install, "MINIMUM_PYTHON", (99, 0))
    blocker = home / "file"
    blocker.write_text("x")
    lines = doctor_lines(capsys)
    assert lines[0].startswith("WARN  Python ") and "(needs 99.0 or newer)" in lines[0]
    assert any(line.startswith(f"WARN  config file {home / 'config.toml'}: not valid TOML") for line in lines)
    assert any(line.startswith("WARN  file locking does not work (two handles got the same lock)") for line in lines)
    assert any(line.startswith("WARN  process list not readable") for line in lines)
    monkeypatch.setenv("QR_STATE", str(blocker / "state"))
    assert any(line.startswith(f"WARN  state directory {blocker / 'state'} cannot be used") for line in doctor_lines(capsys))


def test_lock_check_keeps_its_verdict_when_unlocking_fails(home, monkeypatch):
    """First CI run, Windows: with a lock two handles could both take, unlocking the handle that did not really
    hold it raised, and the report said "PermissionError" instead of what was wrong."""
    from quota_router import install, platform_compat

    def refuse(file):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(platform_compat, "try_lock", lambda file: True)
    monkeypatch.setattr(platform_compat, "unlock", refuse)
    assert install.check_lock(str(home / "state")) == (False, "two handles got the same lock")
    monkeypatch.setattr(platform_compat, "try_lock", lambda file: False)
    assert install.check_lock(str(home / "state")) == (False, "the test lock is held by another process")
    taken = []
    monkeypatch.setattr(platform_compat, "try_lock", lambda file: not taken and not taken.append(file))
    assert install.check_lock(str(home / "state")) == (True, "")          # exclusive: fine even though unlock raised


# ---- the installed command is actually run ------------------------------------------------
def test_the_self_test_event_answers_without_doing_anything(home, write_config, monkeypatch, capsys):
    import io
    from quota_router import hook, install
    for expected in ("missing", "ok", "error"):
        if expected == "ok":
            write_config()
        elif expected == "error":
            write_config("[general\n")
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(install.SELF_TEST_EVENT)))
        hook.main()
        captured = capsys.readouterr()
        assert json.loads(captured.out) == {"quota_router": "ok", "config": expected} and captured.err == ""
    assert sorted(p.name for p in home.iterdir()) == ["config.toml"]       # no state directory, no log, no reservation


def test_doctor_runs_the_installed_command(home, quiet_machine, capsys):
    qr("install")
    capsys.readouterr()
    lines = doctor_lines(capsys)
    assert f"OK    hook installed in {settings(home)}" in lines
    assert f"OK    hook command in {settings(home)} starts and answers" in lines


@pytest.fixture
def broken_entry(home, monkeypatch):
    """Make `qr install` write an entry that starts but does not behave like the hook."""
    from quota_router import install
    package = home / "broken" / "quota_router"
    package.mkdir(parents=True)
    script = package / "__main__.py"
    script.write_text("import sys\nprint('one', file=sys.stderr)\nprint('', file=sys.stderr)\nprint('two ' + 'x' * 500, file=sys.stderr)\n"
                      "print('three', file=sys.stderr)\nprint('four', file=sys.stderr)\nprint('not json')\nsys.exit(3)\n")
    monkeypatch.setattr(install, "ENTRY_FILE", str(script))
    return install


def test_install_warns_when_the_installed_command_does_not_answer(broken_entry, home, capsys):
    with pytest.raises(SystemExit) as stopped:
        qr("install")
    assert stopped.value.code == 1                 # the file is written, but the command in it routes nothing
    captured = capsys.readouterr()
    path = settings(home)
    assert f"Added the quota-router hook to {path}" in captured.out
    lines = captured.err.splitlines()
    assert lines[0] == f"WARN  hook command in {path} does not work: it exited with status 3 without answering in JSON"
    assert lines[1:] == ["WARN    stderr: one", "WARN    stderr: two " + "x" * 196, "WARN    stderr: three"]      # the first few lines, cut short


def test_doctor_warns_when_the_installed_command_does_not_answer(broken_entry, home, quiet_machine, capsys):
    with pytest.raises(SystemExit):
        qr("install")
    capsys.readouterr()
    lines = doctor_lines(capsys)
    path = settings(home)
    assert f"OK    hook installed in {path}" in lines                 # listed, and yet:
    start = lines.index(f"WARN  hook command in {path} does not work: it exited with status 3 without answering in JSON")
    assert lines[start + 1:start + 4] == ["WARN    stderr: one", "WARN    stderr: two " + "x" * 196, "WARN    stderr: three"]


def test_doctor_reports_a_command_that_cannot_start(home, quiet_machine, capsys):
    from quota_router import install
    path = settings(home)
    path.parent.mkdir()
    entry = dict(install.hook_entry(), command=str(home / "no-such-python"))
    path.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [entry]}]}}))
    os.makedirs(os.path.dirname(install.record_path()))
    with open(install.record_path(), "w", encoding="utf-8") as f:
        json.dump({os.path.normcase(str(path)): {"python": entry["command"], "entry": install.ENTRY_FILE}}, f)
    lines = doctor_lines(capsys)
    assert any(line.startswith(f"WARN  hook in {path} points at another Python") for line in lines)
    assert any(line.startswith(f"WARN  hook command in {path} does not work: it could not be started (") for line in lines)


@pytest.mark.skipif(sys.platform == "win32", reason="a shell-form entry is run through /bin/sh here; on Windows it needs Git Bash")
def test_doctor_runs_a_hand_written_entry_too(home, quiet_machine, capsys):
    path = settings(home)
    path.parent.mkdir()
    command = f"{shlex.quote(sys.executable)} {shlex.quote(os.path.join(ROOT, 'bin', 'qr'))} hook"
    path.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": command}]}]}}))
    lines = doctor_lines(capsys)
    assert f"OK    hook present in {path} as a hand-written `qr hook` entry" in lines
    assert f"OK    hook command in {path} starts and answers" in lines
