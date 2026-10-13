"""Nothing that comes from a network response, the config file, the command or a token may reach a log file.

Each scenario pushes a fake token, a fake email address and a fake command through one path that ends in a log
record, then searches every file in the state directory for them.
"""
import io
import json
import re
import sys
import urllib.error

import pytest

from conftest import CONFIG

TOKEN = "FAKE_SECRET_123"
EMAIL = "alice@example.test"
PROMPT = "do the secret thing"
COMMAND = f'API_TOKEN={TOKEN} claude -p "{PROMPT}, then mail {EMAIL}" --model opus; codex exec "{PROMPT}"'
POISON = f"Authorization: Bearer {TOKEN} for {EMAIL} while running {COMMAND}"
NEEDLES = (TOKEN, EMAIL, "alice", PROMPT, "claude -p", "codex exec", "API_TOKEN", "Bearer")
ERROR_LINE = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \| ([a-z-]+|#[0-9a-f]{12}) \| (-|[A-Za-z_][A-Za-z0-9_]*(-\d{3})?) \| "
                        r"(-|[a-z_]+\.py:\d+:[A-Za-z0-9_]+) \| (-|[A-Za-z0-9_.-]+|#[0-9a-f]{12})")
DECISION_LINE = re.compile(r"\d\d-\d\d \d\d:\d\d:\d\d \| (hook|qr pick|qr check|probe|refresh|other) \| lead=[A-Za-z0-9_.#?-]+( \(assumed\))? \| "
                           r"sess=(-|[A-Za-z0-9_.-]{1,8}|#[0-9a-f]{12}) \| (claude|codex|\?|other) \| ([a-z-]+|#[0-9a-f]{12}) \| "
                           r"pick=(-|[A-Za-z0-9_.-]+|#[0-9a-f]{12}) \| [A-Za-z0-9 %.,;:#-]* \| cmd#([0-9a-f]{12}|-)")


@pytest.fixture
def live(router, monkeypatch):
    """The router fixture, but with the real reading code: only the credentials and the network are fake."""
    core = router.core
    monkeypatch.setattr(core, "claude_reading", lambda acc, cache, force=False: core.cached_reading("claude", acc, cache, force))
    monkeypatch.setattr(core, "codex_reading", lambda acc, cache=None, force=False: core.cached_reading("codex", acc, {} if cache is None else cache, force))
    monkeypatch.setattr(core, "claude_token", lambda acc: (f"\n{TOKEN}", None))
    monkeypatch.setattr(core, "codex_identity", lambda acc: {"access_token": f"prefix'{TOKEN}\nx", "account_id": EMAIL})
    monkeypatch.setattr(core, "claude_local_cache", lambda acc: None)
    monkeypatch.setattr(core, "codex_log_reading", lambda acc: {})
    return router


def run_hook(monkeypatch, capsys, command=COMMAND, session=f"{EMAIL}/{TOKEN}"):
    from quota_router import hook
    event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/tmp", "session_id": session}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    hook.main()
    return capsys.readouterr()


def logs(router):
    """{file name: text} of everything in the state directory, after checking that none of it holds a needle
    and that the two log files hold nothing but well-formed records.

    Every file is searched, not only `*.log`, with one exception: leases.json is state, not a log, and keeps the
    session id Claude Code sent so that `qr leases --clear --session ID` can find a session's reservations."""
    state = router.home / "state"
    found = {}
    for path in sorted(p for p in state.rglob("*") if p.is_file() and p.name != "leases.json"):
        text = path.read_text(encoding="utf-8", errors="replace")
        found[path.name] = text
        for needle in NEEDLES:
            assert needle not in text, f"{needle!r} leaked into {path.name}: {text[:400]}"
    for line in found.get("hook-errors.log", "").splitlines():
        assert ERROR_LINE.fullmatch(line), line
    for line in found.get("decisions.log", "").splitlines():
        assert DECISION_LINE.fullmatch(line), line
    return found


def raise_poison(kind):
    def urlopen(request, timeout=None):
        raise kind(POISON)
    return urlopen


def test_network_errors(live, monkeypatch, capsys):
    core = live.core

    def header_error(request, timeout=None):
        import http.client
        connection = http.client.HTTPConnection("example.invalid")
        connection.putrequest("GET", "/")                    # buffered: no connection is opened
        for name, value in request.header_items():
            connection.putheader(name, value)                # ValueError quoting the whole Authorization header
    for urlopen in (header_error, raise_poison(OSError), raise_poison(TimeoutError), raise_poison(RuntimeError),
                    lambda request, timeout=None: (_ for _ in ()).throw(urllib.error.URLError(POISON))):
        (live.home / "state" / "cache.json").unlink(missing_ok=True)       # no cooldown left over from the last one
        monkeypatch.setattr(core, "open_url", urlopen)
        run_hook(monkeypatch, capsys)
    found = logs(live)
    assert found["hook-errors.log"].count(" | usage-query | ") >= 5 and " | usage-query | ValueError | " in found["hook-errors.log"]
    assert found["decisions.log"].count("\n") >= 5


@pytest.mark.parametrize("payload", [
    {"seven_day": {"utilization": f'API_TOKEN={TOKEN} claude -p "{EMAIL}"', "resets_at": None}},
    {"seven_day": {"utilization": 10, "resets_at": POISON}},
    {POISON: 1, "limits": [POISON]},
    POISON,
    {"rate_limit": {"primary_window": {"limit_window_seconds": 604800, "used_percent": POISON, "reset_at": POISON}},
     "credits": {"balance": POISON}},
])
def test_malformed_responses(live, monkeypatch, capsys, payload):
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(live.core, "open_url", lambda request, timeout=None: Response(json.dumps(payload).encode()))
    captured = run_hook(monkeypatch, capsys)
    found = logs(live)
    assert "hook-errors.log" in found or "decisions.log" in found
    for needle in (TOKEN, EMAIL):
        assert needle not in captured.err


def test_response_that_is_not_json(live, monkeypatch, capsys):
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(live.core, "open_url", lambda request, timeout=None: Response(POISON.encode()))
    run_hook(monkeypatch, capsys)
    assert " | usage-query | JSONDecodeError | " in logs(live)["hook-errors.log"]


def test_config_errors(live, write_config, monkeypatch, capsys):
    from quota_router import config
    state = live.home / "state"
    for text in (f'[general]\nlanguage = "{POISON}"\n' + CONFIG, f"{POISON}\n", CONFIG + f'\n[[accounts]]\nfamily = "{TOKEN}"\nkey = "{EMAIL}"\n'):
        write_config(text)
        assert run_hook(monkeypatch, capsys).out == ""          # the diagnostic goes to stderr, never to a file
    monkeypatch.setattr(config, "load", lambda path=None: (_ for _ in ()).throw(RuntimeError(POISON)))
    assert run_hook(monkeypatch, capsys).out == ""
    assert not state.exists() or logs(live) is not None


def test_translation_errors(live, monkeypatch, capsys):
    """A message template that cannot be formatted raises with the template text and the values in it."""
    def broken(key, /, **values):
        raise KeyError(f"{key}: {POISON} {values}")
    monkeypatch.setattr(live.core, "open_url", raise_poison(OSError))
    monkeypatch.setattr(live.core, "t", broken)
    monkeypatch.setattr(live.hook, "t", broken)
    captured = run_hook(monkeypatch, capsys)
    assert captured.err == "quota-router: routing failed (KeyError); command left unchanged\n"
    found = logs(live)
    assert " | hook | KeyError | " in found["hook-errors.log"] and " | fail | pick=- | KeyError | cmd#" in found["decisions.log"]


def test_state_write_errors(live, monkeypatch, capsys):
    from quota_router import platform_compat

    def refuse(source, target):
        raise PermissionError(13, POISON, f"/state/{TOKEN}/{EMAIL}")
    monkeypatch.setattr(platform_compat, "replace", refuse)
    monkeypatch.setattr(live.core, "open_url", raise_poison(OSError))
    run_hook(monkeypatch, capsys)
    found = logs(live)
    assert " | state-save | PermissionError | core.py:" in found["hook-errors.log"] and " | cache.json\n" in found["hook-errors.log"]
    # a lock file that cannot be opened is reported the same way.
    real_open = open

    def no_lock(path, *args, **kwargs):
        if str(path).endswith(".lock"):
            raise OSError(5, POISON, str(path) + TOKEN)
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr("builtins.open", no_lock)
    run_hook(monkeypatch, capsys)
    monkeypatch.setattr("builtins.open", real_open)
    assert " | lock-failed | OSError | core.py:" in logs(live)["hook-errors.log"]


def test_unusual_account_keys_sessions_and_leads(router, write_config, monkeypatch, capsys):
    from quota_router import config
    odd = "帳號é"                                               # valid in the config file, not plain ASCII
    router.core.configure(config.load(write_config(CONFIG.replace('key = "claude-b"', f'key = "{odd}"'))))
    router.usage[odd] = dict(router.usage["claude-b"], email=EMAIL)
    router.usage[odd]["usage"] = {"seven_day": {"used": 1, "reset": router.usage["claude-b"]["usage"]["seven_day"]["reset"]}}
    out = run_hook(monkeypatch, capsys, command="claude -p x --model opus").out
    assert ".claude-b" in json.loads(out)["hookSpecificOutput"]["updatedInput"]["command"]
    router.core.log_decision("hook", POISON, POISON, {"key": POISON, "model": POISON}, text=POISON, cmd=COMMAND,
                             lead=POISON + " (assumed)", session=POISON, model=POISON)
    router.core.log_decision(POISON, "codex", "probe", text=POISON, account=EMAIL)
    router.core.log_error(POISON, RuntimeError(POISON), POISON)
    router.core.log_error("usage-query", None, odd)
    found = logs(router)
    assert odd not in found["decisions.log"] and odd not in found["hook-errors.log"]
    first = found["decisions.log"].splitlines()[0].split(" | ")
    assert first[1:6] == ["hook", "lead=claude-main", "sess=" + router.core.log_token(f"{EMAIL}/{TOKEN}"), "claude", "auto"]
    assert first[6] == "pick=" + router.core.log_token(odd) and re.fullmatch(r"pick=#[0-9a-f]{12}", first[6])
    # the report still shows such an account under its name.
    router.core.decision_statistics(7, router.core.score_rows("claude", router.core.read_family("claude", lead_key="claude-main")))
    assert "alice: auto 1, explicit 0, resumed 0" in capsys.readouterr().out


def test_debug_file_is_separate_and_opt_in(live, monkeypatch, capsys):
    monkeypatch.setattr(live.core, "open_url", raise_poison(OSError))
    run_hook(monkeypatch, capsys)
    state = live.home / "state"
    assert not (state / "debug.log").exists()
    (state / "cache.json").unlink()
    monkeypatch.setenv("QR_DEBUG", "1")
    run_hook(monkeypatch, capsys)
    debug = (state / "debug.log").read_text(encoding="utf-8")
    assert "Traceback" in debug and "usage-query" in debug          # the detail is here, and may be sensitive
    (state / "debug.log").unlink()
    logs(live)                                                      # every other file is still clean


@pytest.mark.parametrize("outcome", ["renewed", "dead", "failed", "unsaved", "limited", "refused", "odd"])
def test_login_renewal(router, write_config, monkeypatch, capsys, outcome):
    """A renewed Claude login: neither the old nor the new tokens, nor anything the token endpoint says, reach a log
    file or the output. The tokens are only ever written to Claude Code's own credential file. The HTTP status code
    of an error answer is logged; its body (error_description included) is not."""
    import test_login_refresh as lr
    core = router.core
    store = lr.make_store(router, write_config, monkeypatch, "file")
    monkeypatch.setattr(core, "claude_reading", lambda acc, cache, force=False: core.cached_reading("claude", acc, cache, force))
    store.put(lr.login(access=f"old-{TOKEN}", refresh=f"refresh-{TOKEN}"))

    def error(code, body):
        return lambda: urllib.error.HTTPError(lr.TOKEN_URL, code, POISON, {"X-Echo": POISON}, io.BytesIO(json.dumps(body).encode()))
    store.answer = {
        "dead": error(400, {"error": "invalid_grant", "error_description": POISON}),
        "limited": error(429, {"error": {"type": "rate_limit_error", "message": POISON}}),
        "refused": error(403, {"error": POISON, "error_description": POISON}),
        "failed": lambda: OSError(POISON),
        "odd": lr.answer(access_token=None, refresh_token=f"rotated-{TOKEN}", scope=POISON),
    }.get(outcome, lr.answer(access_token=f"new-{TOKEN}", refresh_token=f"rotated-{TOKEN}"))
    if outcome == "unsaved":
        real = store.pc.replace
        monkeypatch.setattr(store.pc, "replace", lambda source, target: (_ for _ in ()).throw(PermissionError(13, POISON, target))
                            if target.endswith(".credentials.json") else real(source, target))
    captured = run_hook(monkeypatch, capsys, command='claude -p "summarise" --model opus')
    assert len(store.token_calls) == 1
    for needle in (TOKEN, EMAIL):
        assert needle not in captured.err and needle not in captured.out
    found = logs(router)
    assert " | refresh-started | " in found["decisions.log"]
    assert re.search(r" \| refresh(-(?!started)[a-z-]+)? \| pick=claude-b \| ", found["decisions.log"])    # how it ended
    code = {"dead": 400, "limited": 429, "refused": 403}.get(outcome)
    if code:
        assert f" | login-refresh | HTTPError-{code} | " in found["hook-errors.log"]
    if outcome in ("limited", "refused"):
        assert f"renewal failed, HTTP {code}" in found["decisions.log"]
    saved = store.stored()["claudeAiOauth"]
    assert saved["accessToken"] == (f"new-{TOKEN}" if outcome == "renewed" else f"old-{TOKEN}")
