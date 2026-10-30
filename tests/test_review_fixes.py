"""Regressions for the phase 1 review: quoting of generated commands, what the logs hold, the hook's
outer boundary, `qr init` and config diagnostics."""
import io
import json
import os
import re
import subprocess
import sys

import pytest

from conftest import CONFIG, as_word

posix_shell = pytest.mark.skipif(sys.platform == "win32", reason="runs the generated command through /bin/sh")
AWKWARD = ["Team Account", "a; echo X; #", "it's `id` & $(id) \"q\""]


def configure(router, write_config, text):
    from quota_router import config
    router.core.configure(config.load(write_config(text)))
    now = router.usage["codex-1"]["fetched"]
    for account in router.core.CLAUDE_ACCOUNTS + router.core.CODEX_ACCOUNTS:
        # the lead has used a little more, so which account is roomier never comes down to a tie (see conftest.router).
        router.usage.setdefault(account["key"], {"email": "", "live": True, "fetched": now,
                                                 "usage": {"seven_day": {"used": 5 if account["key"] == "lead" else 1, "reset": now + 86400}}})


def fake_cli(directory):
    """Fake `claude` and `codex` that print their arguments and the account variable, one per line."""
    directory.mkdir()
    for name, variable in (("claude", "CLAUDE_CONFIG_DIR"), ("codex", "CODEX_HOME")):
        path = directory / name
        path.write_text('#!/bin/sh\nfor a in "$@"; do printf "arg:%s\\n" "$a"; done\nprintf "dir:%s\\n" "${' + variable + '-}"\n')
        path.chmod(0o755)
    return {"PATH": f"{directory}:/usr/bin:/bin"}


def sh(argv, env):
    """(exit status, stdout lines, stderr). Popen, because the router fixture blocks subprocess.run."""
    process = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = process.communicate(timeout=30)
    return process.returncode, out.splitlines(), err


@posix_shell
@pytest.mark.parametrize("name", AWKWARD)
def test_routed_command_survives_awkward_directories(router, write_config, name):
    directory = router.home / name
    configure(router, write_config, f'[[accounts]]\nfamily = "claude"\nkey = "lead"\ndir = ""\n'
                                    f"[[accounts]]\nfamily = \"claude\"\nkey = \"team\"\ndir = '''{directory}'''\n"
                                    f"[[accounts]]\nfamily = \"codex\"\nkey = \"cx\"\ndir = '''{directory}'''\n")
    env = fake_cli(router.home / "bin")
    for command, expected in (("claude -p 'hi there' --model opus", ["arg:-p", "arg:hi there", "arg:--model", "arg:opus"]),
                              ("codex exec 'do it'", ["arg:exec", "arg:do it"])):
        routed = router.hook.decide(command, "/tmp", lead_key="lead")["command"]
        assert routed is not None and routed.endswith(command)
        # nothing else ran, nothing was cut off
        assert sh(["/bin/sh", "-c", routed], env) == (0, expected + [f"dir:{directory}"], "")


@posix_shell
@pytest.mark.parametrize("name", AWKWARD)
def test_resumed_command_survives_awkward_directories(router, write_config, name):
    directory = router.home / name
    (directory / "projects" / "p").mkdir(parents=True)
    (directory / "projects" / "p" / "abc123.jsonl").write_text("{}\n")
    configure(router, write_config, f'[[accounts]]\nfamily = "claude"\nkey = "lead"\ndir = ""\n'
                                    f"[[accounts]]\nfamily = \"claude\"\nkey = \"team\"\ndir = '''{directory}'''\n")
    routed = router.hook.decide("claude -p next --resume abc123 --model opus", "/tmp", lead_key="lead")["command"]
    assert sh(["/bin/sh", "-c", routed], fake_cli(router.home / "bin")) == (
        0, ["arg:-p", "arg:next", "arg:--resume", "arg:abc123", "arg:--model", "arg:opus", f"dir:{directory}"], "")


@posix_shell
@pytest.mark.parametrize("name", AWKWARD)
def test_pick_env_output_survives_awkward_directories(router, write_config, capsys, name):
    directory = router.home / name
    configure(router, write_config, f"[[accounts]]\nfamily = \"codex\"\nkey = \"cx\"\ndir = '''{directory}'''\n")
    router.core.pick("codex", env_only=True, strict=True)
    prefix = capsys.readouterr().out.strip()
    assert sh(["/bin/sh", "-c", 'eval "$1 codex exec hello"', "sh", prefix], fake_cli(router.home / "bin")) == (
        0, ["arg:exec", "arg:hello", f"dir:{directory}"], "")


def test_plain_directories_are_left_unquoted(router):
    # the common case reads exactly as before: quoting only appears when a character needs it.
    routed = router.hook.decide("claude -p x --model opus", "/tmp", lead_key="claude-main")["command"]
    # (the expected text is built the way the router writes a directory: forward slashes on Windows too.)
    word = as_word(router.home / ".claude-b")
    assert routed == f"env CLAUDE_CONFIG_DIR={word} claude -p x --model opus" and not word.startswith(("'", '"'))


def test_windows_paths_are_written_with_forward_slashes(monkeypatch):
    from quota_router import platform_compat as pc
    monkeypatch.setattr(pc, "system", lambda: "windows")
    assert pc.shell_quote(r"C:\Users\me\.claude-b") == "C:/Users/me/.claude-b"
    assert pc.shell_quote(r"C:\home\Jo Doe\.claude-b") == "'C:/home/Jo Doe/.claude-b'"


# ---- logs ---------------------------------------------------------------------
SECRET = "FAKE_SECRET_123"


def test_decisions_log_never_holds_the_command(router):
    command = f'API_TOKEN={SECRET} claude -p "email me at alice@example.test" --model opus'
    assert router.hook.decide(command, "/tmp", lead_key="claude-main", session="session-1")["command"]
    for key in router.usage:
        router.usage[key]["usage"]["seven_day"]["used"] = 100
    assert router.hook.decide(command, "/tmp", lead_key="claude-main")["deny"]
    assert router.hook.decide(f"CLAUDE_CONFIG_DIR=/x/{SECRET} claude -p y --model opus", "/tmp", lead_key="claude-main")
    log = (router.home / "state" / "decisions.log").read_text(encoding="utf-8")
    assert log.count("\n") == 3
    assert SECRET not in log and "alice" not in log and "email me" not in log and "claude -p" not in log
    first = log.splitlines()[0].split(" | ")
    assert first[1:6] == ["hook", "lead=claude-main", "sess=session-", "claude", "auto"]
    assert first[6] == "pick=claude-b" and "model other" in first[7]         # the key only: no email, no directory
    assert re.fullmatch(r"cmd#[0-9a-f]{12}", first[8])
    # the same command hashes the same, so repeated dispatches can still be told apart from different ones.
    assert log.splitlines()[1].endswith(first[8]) and not log.splitlines()[2].endswith(first[8])


def test_redact_patterns():
    from quota_router.core import redact
    assert SECRET not in redact(f"ValueError: Invalid header value b'Bearer {SECRET}\\nx'")
    assert redact("Authorization: Bearer abc.def-123") == "Authorization: Bearer [redacted]"
    assert redact("key sk-ant-REDACTME0123456789 used") == "key [redacted] used"
    assert redact("token=abcdefghijklmnopqrstuvwxyz012345") == "token=[redacted]"
    assert redact('{"accessToken": "x"} access_token="abcdefghijklmnopqrstuvwxyz"') == '{"accessToken": "x"} access_token="[redacted]"'
    assert redact("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig") == "[redacted]"
    # things that must stay readable: paths, short values, ordinary traceback lines.
    for text in ("CLAUDE_CONFIG_DIR=/home/someone/.claude-b-with-a-long-name", "retries=3", "dir=~/.codex-b/sessions/2026/10/11",
                 r"CODEX_HOME=C:\home\someone\.codex-with-a-long-name", '  File "/x/quota_router/core.py", line 351, in network_json'):
        assert redact(text) == text


def test_error_log_holds_no_message_on_the_real_failure_path(router, monkeypatch):
    """A token with a newline in it makes http.client quote the whole header in its error message."""
    core = router.core
    monkeypatch.setattr(core, "claude_token", lambda acc: ("\n" + SECRET, None))
    monkeypatch.setattr(core, "claude_local_cache", lambda acc: None)

    def urlopen(request, timeout):
        import http.client
        connection = http.client.HTTPConnection("example.invalid")
        connection.putrequest("GET", "/")             # buffered: no connection is opened
        for name, value in request.header_items():
            connection.putheader(name, value)
    monkeypatch.setattr(core, "open_url", urlopen)
    reading = core.cached_reading("claude", core.CLAUDE_ACCOUNTS[1], {})      # the real one; the fixture replaced claude_reading
    assert "cannot reach the usage endpoint (ValueError)" in reading["error"]
    log = (router.home / "state" / "hook-errors.log").read_text(encoding="utf-8")
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \| usage-query \| ValueError \| core\.py:\d+:fetch \| claude-b\n", log)
    assert not (router.home / "state" / "debug.log").exists()               # only with QR_DEBUG=1


def test_hook_error_log_holds_no_message(router, monkeypatch, capsys):
    def explode(*args, **kwargs):
        raise RuntimeError(f"upstream said Authorization: Bearer {SECRET}")
    monkeypatch.setattr(router.hook, "decide", explode)
    out, err = run_main(monkeypatch, capsys)
    assert err == "quota-router: routing failed (RuntimeError); command left unchanged\n"
    assert "[quota-router] Routing failed (RuntimeError); running the command as written." in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    state = router.home / "state"
    assert SECRET not in (state / "hook-errors.log").read_text() and SECRET not in (state / "decisions.log").read_text()
    assert " | hook | RuntimeError | " in (state / "hook-errors.log").read_text()
    assert " | fail | pick=- | RuntimeError | cmd#" in (state / "decisions.log").read_text()


# ---- the hook's outer boundary ---------------------------------------------------
EVENT = {"tool_name": "Bash", "tool_input": {"command": "claude -p hi --model opus"}, "cwd": "/tmp", "session_id": "s1"}


def run_main(monkeypatch, capsys, event=EVENT):
    from quota_router import hook
    monkeypatch.setattr(sys, "stdin", io.StringIO(event if isinstance(event, str) else json.dumps(event)))
    hook.main()
    captured = capsys.readouterr()
    return captured.out, captured.err


def boom(*args, **kwargs):
    raise RuntimeError("boom")


@pytest.mark.parametrize("event", ['["unexpected shape"]', '"text"', "7", "null",
                                   '{"tool_name": "Bash", "tool_input": ["claude"]}',
                                   '{"tool_name": "Bash", "tool_input": {"command": ["claude -p x"]}}'])
def test_unexpected_input_shapes_are_ignored(home, monkeypatch, capsys, event):
    assert run_main(monkeypatch, capsys, event) == ("", "")


def test_odd_field_types_still_route(router, monkeypatch, capsys):
    out, err = run_main(monkeypatch, capsys, dict(EVENT, cwd=7, session_id=["x"], transcript_path=None))
    assert err == "" and json.loads(out)["hookSpecificOutput"]["updatedInput"]["command"].endswith("claude -p hi --model opus")


def test_failure_while_reporting_a_failure_still_fails_open(router, monkeypatch, capsys):
    monkeypatch.setattr(router.hook, "decide", boom)
    monkeypatch.setattr(router.hook, "t", lambda *a, **k: (_ for _ in ()).throw(KeyError("note.failed")))
    monkeypatch.setattr(router.core, "recent_failures", boom)
    out, err = run_main(monkeypatch, capsys)
    assert err == "quota-router: routing failed (RuntimeError); command left unchanged\n"
    decision = json.loads(out)["hookSpecificOutput"]
    assert "updatedInput" not in decision and "permissionDecision" not in decision
    assert decision["additionalContext"] == "[quota-router] Routing failed (RuntimeError); running the command as written."


def test_a_decided_deny_is_still_delivered_when_the_failure_count_breaks(router, monkeypatch, capsys):
    monkeypatch.setattr(router.hook, "decide", lambda *a, **k: {"deny": "quota exhausted"})
    monkeypatch.setattr(router.core, "recent_failures", boom)
    out, err = run_main(monkeypatch, capsys)
    decision = json.loads(out)["hookSpecificOutput"]
    assert err == "" and decision["permissionDecision"] == "deny" and decision["permissionDecisionReason"] == "quota exhausted"


@pytest.mark.parametrize("target", ["detect_lead", "configure"])
def test_any_other_exception_leaves_the_command_alone(router, monkeypatch, capsys, target):
    monkeypatch.setattr(router.core, target, boom)
    out, err = run_main(monkeypatch, capsys)
    assert "updatedInput" not in out and "permissionDecision" not in out
    assert err.count("\n") == 1 and err.startswith("quota-router: ") and err.endswith("command left unchanged\n")


def test_exception_while_writing_the_decision_is_contained(router, monkeypatch, capsys):
    monkeypatch.setattr(router.hook, "write_output", boom)
    out, err = run_main(monkeypatch, capsys)
    assert out == "" and err == "quota-router: internal error (RuntimeError); command left unchanged\n"
    assert " | hook | RuntimeError | hook.py:" in (router.home / "state" / "hook-errors.log").read_text()
    assert router.core.recent_failures() == 1


# ---- qr init and config diagnostics -------------------------------------------------
def test_init_gives_colliding_directories_unique_keys(home, monkeypatch, capsys):
    from quota_router import cli, config
    for name in (".claude-main/projects", ".claude-main-2/projects", ".claude-b c/projects", ".codex-1/sessions"):
        (home / name).mkdir(parents=True)
    cli.main(["init"])
    loaded = config.load()                                    # what was written loads
    keys = [a["key"] for a in loaded.accounts]
    assert len(keys) == len(set(keys)) == 8
    assert [(a["key"], a["raw_dir"]) for a in loaded.family("claude")] == [
        ("claude-main", ""), ("claude-b-c", "~/.claude-b c"), ("claude-main-2", "~/.claude-main"), ("claude-main-2-2", "~/.claude-main-2")]
    assert "claude-main-2" in capsys.readouterr().out


def test_init_writes_nothing_when_the_result_would_not_load(home, monkeypatch, capsys):
    from quota_router import cli, config
    monkeypatch.setattr(config, "detect_accounts", lambda home=None: [{"family": "claude", "key": "x", "dir": ""}] * 2)
    with pytest.raises(SystemExit) as stop:
        cli.main(["init"])
    assert stop.value.code == 2 and 'account "x" is listed twice' in capsys.readouterr().err
    assert not os.path.exists(config.config_path()[0])


def test_undecodable_config_is_a_config_error(write_config, home, monkeypatch, capsys):
    from quota_router import cli, config
    path = write_config()
    path.write_bytes(b"\xff")
    with pytest.raises(config.ConfigError, match="not UTF-8"):
        config.load()
    with pytest.raises(SystemExit) as stop:
        cli.main(["config"])
    assert stop.value.code == 2
    assert capsys.readouterr().err == f"quota-router: config error in {path}: not valid TOML: the file is not UTF-8 text\n"
    out, err = run_main(monkeypatch, capsys)
    assert out == "" and err.count("\n") == 1 and "not UTF-8" in err


def test_config_diagnostics_stay_on_one_line(write_config, monkeypatch, capsys):
    write_config(CONFIG + '\n[[accounts]]\nfamily = "claude"\nkey = "bad\\nkey\\u001b[31m"\n')
    out, err = run_main(monkeypatch, capsys)
    assert out == "" and err.count("\n") == 1 and "\x1b" not in err and "bad key" in err
