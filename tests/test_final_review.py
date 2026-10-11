"""Regressions for the final review: what counts as a dispatch, which account a command names, the paths that
could spend money, redirects, the hard time limit, accounts nobody is logged in to, and config-file diagnostics."""
import http.server
import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from conftest import CONFIG, ROOT, WINDOWS, as_word, child_env


def decide(router, command):
    return router.hook.decide(command, "/tmp")


def text_of(result):
    return result.get("deny") or "\n".join(result.get("notes", []))


def week(used, now, **windows):
    return dict({"seven_day": {"used": used, "reset": now + 86400}}, **windows)


# ---- 1. heredocs and `--` after an interpreter ---------------------------------------------
@pytest.mark.parametrize("command", [
    "cat <<'123'\nclaude -p hi\n123",                              # review reproduction: a delimiter starting with a digit
    "cat <<END-TEXT\nclaude -p hi\nEND-TEXT",                      # review reproduction: a delimiter with a dash
    "cat <<-\"END OF TEXT\"\n\tcodex exec x\n\tEND OF TEXT",
    "cat <<\\EOF\nclaude -p hi\nEOF",
    "cat <<1.5 >notes.txt\ncodex exec hi\n1.5\n",
    "cat <<''\nclaude -p hi\n\necho done",
    'python -c "print(1)" -- claude -p hi',                        # review reproduction: these are Python's arguments
    "python3 detach.py a b -- claude -p hi",
    "node run.js -- codex exec x",
    "bash wrap.sh -- codex exec x",
])
def test_text_that_is_only_data_is_left_alone(router, command):
    assert router.hook.find_dispatches(command) == []
    assert decide(router, command) is None


def test_a_dispatch_after_a_heredoc_is_routed_and_the_body_is_untouched(router):
    body = "cat <<'123' >prompt.txt\nclaude -p hi\n123\n"
    routed = decide(router, body + "claude -p real --model opus")["command"]
    assert routed.startswith(body + "env CLAUDE_CONFIG_DIR=") and routed.endswith(" claude -p real --model opus")
    assert routed.count("CLAUDE_CONFIG_DIR=") == 1


@pytest.mark.parametrize("command", [
    "cat <<<EOF\nclaude -p x --model opus",                        # a here-string has no body
    "echo $((1<<2))\nclaude -p x --model opus\n2",                 # a shift inside arithmetic is not a heredoc
    "env -- claude -p x --model opus",
    "timeout -k 5 -- 60 claude -p x --model opus",
    "nice -n 5 -- claude -p x --model opus",
])
def test_real_dispatches_next_to_those_are_still_seen(router, command):
    found = router.hook.find_dispatches(command)
    assert len(found) == 1 and command[found[0]["pos"]:].startswith("claude -p x")


# ---- 2. a variable that was cleared names no account ---------------------------------------
CLEARED = ["export CODEX_HOME={b}; env -i codex exec hi",              # review reproduction
           "export CODEX_HOME={b}; env --ignore-environment codex exec hi",
           "export CODEX_HOME={b}; env -u CODEX_HOME codex exec hi",
           "export CODEX_HOME={b}; unset CODEX_HOME; codex exec hi",
           "CODEX_HOME={b} env -i codex exec hi"]


@pytest.mark.parametrize("template", CLEARED)
def test_a_cleared_variable_is_routed_like_no_account_at_all(router, template):
    now = router.usage["codex-1"]["fetched"]
    main, other = (as_word(account["dir"]) for account in router.core.CODEX_ACCOUNTS)
    command = template.format(b=other)
    # the default account is out of subscription quota and has credits: running there unnoticed would charge.
    router.usage["codex-1"].update(credits=40, usage=week(100, now))
    result = decide(router, command)
    assert not result.get("deny")
    assert result["command"] == command.replace("codex exec", f"env CODEX_HOME={other} codex exec")
    assert "Automatically routed to" in text_of(result)
    # and the other way round: the account that was named and then cleared is the expensive one.
    router.usage["codex-1"].update(credits=0, usage=week(10, now))
    router.usage["codex-2"].update(credits=40, usage=week(100, now))
    result = decide(router, command)
    assert not result.get("deny")
    assert result["command"] == command.replace("codex exec", f"env CODEX_HOME={main} codex exec")


def test_an_assignment_after_the_clearing_still_names_the_account(router):
    other = as_word(router.core.CODEX_ACCOUNTS[1]["dir"])
    for command in (f"env -u CODEX_HOME CODEX_HOME={other} codex exec hi", f"unset CODEX_HOME; CODEX_HOME={other} codex exec hi"):
        result = decide(router, command)
        assert result["command"] is None and "Automatically routed" not in text_of(result)


def test_env_u_still_names_the_default_claude_account(router):
    """`env -u CLAUDE_CONFIG_DIR` is what `qr pick claude` and the hook itself write for the account at the default
    location, so on the dispatch itself it names that account; every other way of clearing the variable does not."""
    named = decide(router, "env -u CLAUDE_CONFIG_DIR claude -p x --model opus")
    assert named["command"] is None and "Automatically routed" not in text_of(named)
    other = as_word(router.core.CLAUDE_ACCOUNTS[1]["dir"])
    named = decide(router, f"export CLAUDE_CONFIG_DIR={other}; env -u CLAUDE_CONFIG_DIR claude -p x --model opus")
    assert named["command"] is None and "Automatically routed" not in text_of(named)
    for command in ("unset CLAUDE_CONFIG_DIR; claude -p x --model opus", "env -i claude -p x --model opus",
                    f"export CLAUDE_CONFIG_DIR={other}; env -i claude -p x --model opus"):
        routed = decide(router, command)
        assert routed["command"] is not None and "Automatically routed to" in text_of(routed), command


# ---- 3. the three ways around the credit check ----------------------------------------------
def test_codex_local_fallback_with_an_empty_window_and_credits_is_blocked(router):
    """Review reproduction: weekly 70% left, five-hour 0%, 40 credits, and no live flags to say what a dispatch costs."""
    now = router.usage["codex-2"]["fetched"]
    other = as_word(router.core.CODEX_ACCOUNTS[1]["dir"])
    router.usage["codex-2"].update(live=False, credits=40,
                                   usage=week(30, now, five_hour={"used": 100, "reset": now + 3600}))
    blocked = decide(router, f"CODEX_HOME={other} codex exec hi")
    assert "purchased credits" in blocked["deny"] and "credit balance 40" in blocked["deny"]
    assert "QR_ALLOW_CREDITS=1" in blocked["deny"]
    allowed = decide(router, f"QR_ALLOW_CREDITS=1 CODEX_HOME={other} codex exec hi")
    assert not allowed.get("deny") and allowed["command"] is None
    # routing on its own simply goes to the other account.
    assert f"CODEX_HOME={as_word(router.core.CODEX_ACCOUNTS[0]['dir'])} " in decide(router, "codex exec hi")["command"]
    # a live reading says for itself whether the subscription still covers a dispatch.
    router.usage["codex-2"].update(live=True, allowed=True, limit_reached=False)
    assert not decide(router, f"CODEX_HOME={other} codex exec hi").get("deny")


@pytest.fixture
def conversation(router):
    """A conversation `abc` held by claude-b, whose scarce-model bucket is used up while the shared one has room."""
    project = router.home / ".claude-b" / "projects" / "p"
    project.mkdir(parents=True)
    (project / "abc.jsonl").write_text("{}\n")
    now = router.usage["claude-b"]["fetched"]
    router.usage["claude-b"]["usage"] = week(20, now, fable={"used": 100, "reset": now + 86400})
    return router


def test_resume_without_a_model_cannot_spend_paid_overage_unnoticed(conversation):
    """Review reproduction: shared weekly 80%, scarce-model weekly 0%, paid overage on, no --model."""
    router = conversation
    router.usage["claude-b"]["usage"]["extra_usage"] = {"is_enabled": True}
    blocked = decide(router, "claude -p next --resume abc")
    assert "paid overage" in blocked["deny"] and "QR_ALLOW_CREDITS=1" in blocked["deny"]
    assert not decide(router, "claude -p next --resume abc --model opus").get("deny")     # that model has room
    assert "deny" in decide(router, "claude -p next --resume abc --model fable")
    agreed = decide(router, "QR_ALLOW_CREDITS=1 claude -p next --resume abc")
    assert not agreed.get("deny") and "paid overage" in text_of(agreed)


def test_resume_without_a_model_and_without_overage_keeps_its_account(conversation):
    result = decide(conversation, "claude -p next --resume abc")
    assert not result.get("deny") and ".claude-b" in result["command"]
    assert "If this conversation was using Fable, it may fail" in text_of(result)


@pytest.mark.parametrize("options", [["--env"], ["--env", "--strict"], ["--env", "--peek"], []])
def test_pick_never_hands_out_an_account_that_would_charge(router, capsys, options):
    """Review reproduction: `qr pick codex --env` used to print an account that draws on credits."""
    now = router.usage["codex-1"]["fetched"]
    for key in ("codex-1", "codex-2"):
        router.usage[key].update(credits=40, usage=week(100, now))
    with pytest.raises(SystemExit) as stopped:
        router.core.main(["pick", "codex", *options])
    captured = capsys.readouterr()
    assert stopped.value.code == 3 and captured.out == ""
    assert "purchased credits" in captured.err


def test_strict_is_accepted_and_changes_nothing(router, capsys):
    outputs = []
    for options in (["--env", "--peek"], ["--env", "--peek", "--strict"]):
        router.core.main(["pick", "codex", *options])
        outputs.append(capsys.readouterr().out)
    assert outputs[0] == outputs[1] and outputs[0].startswith("CODEX_HOME=")
    router.core.main(["--help"])
    assert "--strict is accepted for compatibility and changes nothing" in capsys.readouterr().out


# ---- 4. redirects ----------------------------------------------------------------------------
class Recorder(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.seen.append((self.path, self.headers.get("Authorization")))
        if self.server.redirect_to:
            self.send_response(self.server.status)
            self.send_header("Location", self.server.redirect_to)
            self.end_headers()
            return
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def servers():
    """Two loopback servers: `first` answers with a redirect to `second`, which records what reaches it."""
    started = []

    def start():
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
        server.seen, server.redirect_to, server.status = [], None, 302
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
        return server
    first, second = start(), start()
    yield first, second
    for server in started:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_never_followed(servers, status):
    from quota_router import core
    first, second = servers
    first.status, first.redirect_to = status, f"http://127.0.0.1:{second.server_address[1]}/other"
    request = urllib.request.Request(f"http://127.0.0.1:{first.server_address[1]}/usage",
                                     headers={"Authorization": "Bearer FAKE_REVIEW_TOKEN"})
    with pytest.raises(urllib.error.HTTPError) as failed:
        core.network_json(request)
    assert failed.value.code == status
    assert first.seen == [("/usage", "Bearer FAKE_REVIEW_TOKEN")]
    assert second.seen == []                               # the token went to the configured address and nowhere else


def test_a_redirect_to_the_same_server_is_not_followed_either(servers):
    from quota_router import core
    first, _ = servers
    first.redirect_to = "/elsewhere"
    with pytest.raises(urllib.error.HTTPError):
        core.network_json(urllib.request.Request(f"http://127.0.0.1:{first.server_address[1]}/usage"))
    assert [path for path, _ in first.seen] == ["/usage"]


def test_a_redirected_usage_query_is_a_failed_query(router, servers, monkeypatch):
    """Through the real reading path: the account ends up with an error and no numbers, and nothing is retried elsewhere."""
    core = router.core
    first, second = servers
    first.redirect_to = f"http://127.0.0.1:{second.server_address[1]}/other"
    account = core.CODEX_ACCOUNTS[1]
    os.makedirs(account["dir"])
    with open(os.path.join(account["dir"], "auth.json"), "w", encoding="utf-8") as f:
        json.dump({"tokens": {"access_token": "FAKE_REVIEW_TOKEN", "account_id": "acct"}}, f)
    monkeypatch.setattr(core, "open_url", lambda request, timeout: core.OPENER.open(request, timeout=timeout))
    monkeypatch.setitem(core.ENDPOINTS, "codex_usage", f"http://127.0.0.1:{first.server_address[1]}/usage")
    reading = core.cached_reading("codex", account, {})
    assert not reading.get("usage") and reading.get("error") and not reading.get("live")
    assert len(first.seen) == 1 and second.seen == []


# ---- 6. the time limit covers parsing and anything that hangs ---------------------------------
def test_reading_a_very_long_command_takes_linear_time(router):
    """Review reproduction: 100 000 characters took 21 seconds."""
    command = "x " * 50000 + "; claude -p hi --model opus"
    start = time.monotonic()
    found = router.hook.find_dispatches(command)
    assert len(found) == 1 and time.monotonic() - start < 3
    start = time.monotonic()
    assert router.hook.find_dispatches("claude " * 10000) == []
    assert router.hook.mask("a <<b " * 20000)
    assert time.monotonic() - start < 5


def test_a_command_that_cannot_be_read_in_time_runs_as_written(router, monkeypatch, capsys):
    command = "claude -p x --model opus; " * 50
    monkeypatch.setattr(router.hook, "parse_limit", lambda: time.monotonic() - 1)
    with pytest.raises(router.core.BudgetTimeout):
        router.hook.find_dispatches(command)
    answer = router.hook.answer({"session_id": "s1", "cwd": "/tmp"}, {"command": command}, command)
    assert "updatedInput" not in answer and "permissionDecision" not in answer
    assert answer["additionalContext"].startswith("[quota-router] Routing failed (BudgetTimeout)")
    assert capsys.readouterr().err == "quota-router: routing failed (BudgetTimeout); command left unchanged\n"


class StuckStdin:
    """A stdin whose read does not return until `release` is set."""
    def __init__(self, release):
        self.buffer = self
        self.release = release

    def read(self):
        self.release.wait(10)
        return b""


def test_the_watchdog_fires_when_the_hook_is_stuck(home, monkeypatch):
    from quota_router import hook
    fired = threading.Event()
    monkeypatch.setattr(hook, "hard_stop", fired.set)
    monkeypatch.setattr(hook, "WATCHDOG_GRACE_SEC", 0.05)
    monkeypatch.setitem(hook.config.DEFAULTS, "timing.hook_budget_seconds", 0.05)
    monkeypatch.setattr(sys, "stdin", StuckStdin(fired))
    start = time.monotonic()
    hook.main()
    assert fired.is_set() and time.monotonic() - start < 5


def test_the_watchdog_is_cancelled_when_the_hook_answers(router, monkeypatch, capsys):
    fired = threading.Event()
    monkeypatch.setattr(router.hook, "hard_stop", fired.set)
    armed = []
    real_arm = router.hook.Watchdog.arm
    monkeypatch.setattr(router.hook.Watchdog, "arm", lambda self, budget: (real_arm(self, budget), armed.append(self.timer))[0])
    event = {"tool_name": "Bash", "tool_input": {"command": "claude -p hi --model opus"}, "cwd": "/tmp", "session_id": "s1"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(router.core, "configure", lambda cfg: None)      # keep the fixture's fake readings and the short budget
    router.hook.main()
    assert "updatedInput" in json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert len(armed) == 2                                   # once for stdin and the config file, once for the configured budget
    for timer in armed:
        timer.join(5)
        assert not timer.is_alive() and timer.finished.is_set()      # cancelled, not left to run out
    assert not fired.is_set()


STUCK_CHILD = """
import sys, time
from quota_router import hook
hook.WATCHDOG_GRACE_SEC = 0.2
hook.config.DEFAULTS["timing.hook_budget_seconds"] = 0.1
class Never:
    buffer = None
    def read(self):
        time.sleep(120)
Never.buffer = Never()
sys.stdin = Never()
hook.main()
print("the hook returned")
"""


def test_a_stuck_hook_process_ends_without_a_decision(home):
    start = time.monotonic()
    result = subprocess.run([sys.executable, "-c", STUCK_CHILD], env=child_env(home, PYTHONPATH=ROOT),
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert (result.returncode, result.stdout) == (0, "")      # no decision: Claude Code runs the command as written
    assert result.stderr == "quota-router: time limit reached; command left unchanged\n"
    assert time.monotonic() - start < 30


@pytest.mark.parametrize("value", ["inf", "nan", "-1"])
def test_the_budget_must_be_a_finite_number(value):
    from quota_router import config
    with pytest.raises(config.ConfigError) as failed:
        config.parse_text(f"[timing]\nhook_budget_seconds = {value}\n" + CONFIG)
    assert "timing.hook_budget_seconds must be a non-negative number" in failed.value.detail


# ---- 7. an account nobody is logged in to ------------------------------------------------------
def test_an_account_without_a_login_is_never_picked(router):
    """Review reproduction: `qr init` lists the default locations whether or not anybody is logged in there."""
    now = router.usage["codex-2"]["fetched"]
    router.usage["codex-1"] = {"usage": None, "error": "no login", "no_login": True}
    assert f"CODEX_HOME={as_word(router.core.CODEX_ACCOUNTS[1]['dir'])} " in decide(router, "codex exec hi")["command"]
    router.usage["codex-2"]["usage"] = week(97, now)             # the only real account is below the floor
    blocked = decide(router, "codex exec hi")
    assert "No account can take this dispatch" in blocked["deny"]
    assert "codex-1: not logged in (no credentials found for this account)" in blocked["deny"]
    rows = router.core.evaluate("codex")
    assert router.core.choose("codex", rows)[0] is None
    # an account that merely could not be read keeps its middle score, as before.
    router.usage["codex-1"] = {"usage": None, "error": "cannot reach the usage endpoint (OSError)"}
    assert f"CODEX_HOME={as_word(router.core.CODEX_ACCOUNTS[0]['dir'])} " in decide(router, "codex exec hi")["command"]


def test_a_directory_without_credentials_is_marked_and_a_login_clears_the_mark(router, monkeypatch):
    core = router.core
    account = core.CODEX_ACCOUNTS[1]
    reading = core.cached_reading("codex", account, {})
    assert reading.get("no_login") is True and not reading.get("usage")
    assert core.load("cache.json", {})[account["key"]]["no_login"] is True
    os.makedirs(account["dir"])
    with open(os.path.join(account["dir"], "auth.json"), "w", encoding="utf-8") as f:
        json.dump({"tokens": {"access_token": "token", "account_id": "acct"}}, f)
    payload = {"rate_limit": {"allowed": True, "limit_reached": False,
                              "secondary_window": {"limit_window_seconds": 604800, "used_percent": 12, "reset_at": time.time() + 86400}},
               "credits": {"balance": 0}}
    monkeypatch.setattr(core, "open_url", lambda request, timeout: io.BytesIO(json.dumps(payload).encode()))
    reading = core.cached_reading("codex", account, {})
    assert reading["usage"]["seven_day"]["used"] == 12 and "no_login" not in reading
    assert "no_login" not in core.load("cache.json", {})[account["key"]]


def test_no_reading_at_all_is_not_called_live(router):
    router.usage["codex-1"] = {"usage": None}
    router.usage["codex-2"] = {"usage": None}
    rows = router.core.evaluate("codex")
    assert router.core.family_summary("codex", rows).endswith("(reading age: age unknown)")


# ---- 8. which settings.json entries are ours ---------------------------------------------------
def test_a_recorded_entry_is_matched_by_its_whole_argument_list(home):
    from quota_router import install
    path = home / ".claude" / "settings.json"
    recorded = ["-I", "/old/site-packages/quota_router/__main__.py", "hook"]
    os.makedirs(os.path.dirname(install.record_path()))
    with open(install.record_path(), "w", encoding="utf-8") as f:
        json.dump({os.path.normcase(str(path)): {"python": "/old/python3", "args": recorded}}, f)
    pairs = install.known_pairs(str(path))
    entry = {"type": "command", "command": "/old/python3", "args": recorded}
    assert install.is_ours(entry, pairs) and install.is_ours(install.hook_entry(), pairs)
    for args in (["-c", "print(1)", recorded[1], "hook"], recorded + ["extra"], recorded[1:], ["-I", recorded[1], "x", "hook"]):
        assert not install.is_ours(dict(entry, args=args), pairs)
    assert not install.is_ours(dict(entry, command="/usr/bin/printf"), pairs)
    assert install.known_pairs(str(home / "elsewhere" / "settings.json")) is None


def test_without_a_record_only_the_exact_invocation_is_ours():
    from quota_router import install
    ours = install.hook_entry()
    assert install.is_ours(ours) and install.is_ours(dict(ours, command="/other/venv/bin/python3.12"))
    assert install.is_ours(dict(ours, command="C:\\Py\\python.exe"))
    assert not install.is_ours(dict(ours, command="/usr/bin/printf"))
    # review reproduction: this runs print(1); the entry file is merely one of its arguments.
    assert not install.is_ours(dict(ours, args=["-c", "print(1)", install.ENTRY_FILE, "hook"]))
    assert not install.is_ours(dict(ours, args=[install.ENTRY_FILE, "hook"]))


# ---- 9. Git Bash drive paths in the config file -------------------------------------------------
def test_git_bash_drive_paths_are_read_as_drive_paths_on_windows(monkeypatch):
    from quota_router import config, platform_compat
    if not WINDOWS:
        assert platform_compat.drive_path("/c/work/.codex-b") == "/c/work/.codex-b"      # an ordinary path elsewhere
    monkeypatch.setattr(platform_compat, "system", lambda: "windows")
    assert platform_compat.drive_path("/c/work/.codex-b") == "c:/work/.codex-b"
    assert platform_compat.drive_path("/c") == "c:/"
    assert platform_compat.drive_path("/tmp/x") == "/tmp/x" and platform_compat.drive_path("C:/work") == "C:/work"
    assert config.expand_dir("/d/accounts/.claude-b").replace("\\", "/") == "d:/accounts/.claude-b"


@pytest.mark.skipif(not WINDOWS, reason="needs Windows path rules: a drive path is only absolute there")
def test_a_config_file_may_spell_a_directory_the_git_bash_way():
    from quota_router import config
    text = ('[[accounts]]\nfamily = "codex"\nkey = "a"\ndir = "/c/accounts/.codex-b"\n'
            '[[accounts]]\nfamily = "codex"\nkey = "b"\ndir = "C:/accounts/.codex-c"\n')
    first, second = config.parse_text(text).accounts
    assert first["dir"].lower() == "c:\\accounts\\.codex-b" and second["dir"].lower() == "c:\\accounts\\.codex-c"


# ---- 10 and 11. smaller gaps ---------------------------------------------------------------------
def test_an_upper_case_executable_name_is_routed(router, monkeypatch, capsys):
    event = {"tool_name": "Bash", "tool_input": {"command": "CLAUDE.EXE -p hi --model opus"}, "cwd": "/tmp", "session_id": "s1"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(router.core, "configure", lambda cfg: None)      # keep the fixture's fake readings
    router.hook.main()
    command = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["updatedInput"]["command"]
    assert command.startswith("env CLAUDE_CONFIG_DIR=") and command.endswith(" CLAUDE.EXE -p hi --model opus")


def test_unknown_account_fields_and_family_sections_are_reported():
    from quota_router import config
    text = CONFIG.replace('key = "claude-b"', 'key = "claude-b"\nlabel = "team"') + '\n[families.codex]\nscarce_model = "x"\n'
    warnings = config.parse_text(text).warnings
    assert 'unknown field "label" in account "claude-b" (ignored)' in warnings
    assert "unknown section [families.codex] (ignored)" in warnings
    assert config.parse_text(CONFIG).warnings == []
