"""quota-router renews an expired Claude login with its refresh token and writes it back where Claude Code keeps it.

Nothing here reaches the network, the real keychain or the real process table: the token and usage endpoints are a
fake `open_url`, `security` is a fake runner over a dict, and the process list is a dict.
"""
import io
import json
import os
import re
import stat
import time
import types
import urllib.error

import pytest

from conftest import CONFIG, WINDOWS

TOKEN_URL = "http://127.0.0.1:9/token"
USAGE_URL = "http://127.0.0.1:9/usage"
PROFILE_URL = "http://127.0.0.1:9/profile"
ENDPOINTS = f'[endpoints]\nclaude_usage = "{USAGE_URL}"\nclaude_profile = "{PROFILE_URL}"\nclaude_token = "{TOKEN_URL}"\n'
OLD_ACCESS, OLD_REFRESH = "old-access-FAKE", "old-refresh-FAKE"
NEW_ACCESS, NEW_REFRESH = "new-access-FAKE", "new-refresh-FAKE"
SCOPES = ["user:inference", "user:profile", "user:sessions:claude_code"]
MCP = {"github|abc": {"accessToken": "mcp-token", "expiresAt": 0, "serverUrl": "https://example.test/mcp"}}
USAGE = {"five_hour": {"utilization": 10, "resets_at": "2099-01-01T00:00:00Z"},
         "seven_day": {"utilization": 2, "resets_at": "2099-01-05T00:00:00Z"}}


def login(access=OLD_ACCESS, refresh=OLD_REFRESH, expires_in=-3600, **extra):
    oauth = {"accessToken": access, "refreshToken": refresh, "expiresAt": int((time.time() + expires_in) * 1000),
             "scopes": SCOPES, "subscriptionType": "max", "rateLimitTier": "default_claude_max_20x"}
    oauth.update(extra)
    return {"claudeAiOauth": oauth, "mcpOAuth": MCP}


def answer(**override):
    data = {"access_token": NEW_ACCESS, "refresh_token": NEW_REFRESH, "expires_in": 28800,
            "scope": " ".join(SCOPES), "token_type": "Bearer"}
    data.update(override)
    return {k: v for k, v in data.items() if v is not None}


def dead_answer():
    return urllib.error.HTTPError(TOKEN_URL, 400, "Bad Request", {},
                                  io.BytesIO(b'{"error": "invalid_grant", "error_description": "Refresh token not found or invalid"}'))


def result(code=0, out=""):
    return types.SimpleNamespace(returncode=code, stdout=out, stderr="")


class Store:
    """The fake machine: Claude Code's credential store (a file, or keychain items), the endpoints and the process list."""

    def __init__(self, router, mode, monkeypatch):
        from quota_router import platform_compat
        self.core, self.pc, self.mode, self.home = router.core, platform_compat, mode, router.home
        self.acc = next(a for a in self.core.CLAUDE_ACCOUNTS if a["key"] == "claude-b")
        self.dir = self.acc["dir"]
        os.makedirs(self.dir, exist_ok=True)
        self.items = {}                  # (service, account) → JSON text
        self.token_calls, self.used, self.argv = [], [], []
        self.answer = answer()
        self.during_token = None
        self.procs, self.envs = {}, {}
        self.fail_write = False
        monkeypatch.setattr(self.core, "run_local", self.run)
        monkeypatch.setattr(self.core, "open_url", self.open_url)
        monkeypatch.setattr(platform_compat, "list_processes", lambda run: dict(self.procs))
        monkeypatch.setattr(platform_compat, "process_environment", lambda pid, command, run: self.envs.get(pid))
        monkeypatch.setattr(platform_compat, "login_names", lambda: ["tester"])
        monkeypatch.setattr(self.core, "claude_local_cache", lambda acc: None)

    @property
    def service(self):
        return self.pc.keychain_service(self.dir)

    def put(self, data, account="tester"):
        text = json.dumps(data)
        if self.mode == "file":
            with open(os.path.join(self.dir, ".credentials.json"), "w", encoding="utf-8") as f:
                f.write(text)
        else:
            self.items[(self.service, account)] = text

    def stored(self, account="tester"):
        if self.mode == "file":
            with open(os.path.join(self.dir, ".credentials.json"), encoding="utf-8") as f:
                return json.load(f)
        return json.loads(self.items[(self.service, account)])

    def run(self, argv, input=None):
        self.argv.append(list(argv))
        if argv[:2] == ["security", "find-generic-password"]:
            args = argv[2:]
            account = args[args.index("-a") + 1] if "-a" in args else None
            service = args[args.index("-s") + 1]
            found = [(acct, text) for (svc, acct), text in self.items.items()
                     if svc == service and (account is None or acct == account)]
            if not found:
                return result(44)
            acct, text = found[0]
            if "-w" in args:
                return result(0, text + "\n")
            return result(0, f'keychain: "/x/login.keychain-db"\nattributes:\n    "acct"<blob>="{acct}"\n    "svce"<blob>="{service}"\n')
        if argv == ["security", "-i"]:
            if self.fail_write:
                return result(1)
            m = re.fullmatch(r'add-generic-password -U -a "([^"]*)" -s "([^"]*)" -X "([0-9a-f]*)"\n', input)
            assert m, "unexpected security -i input"
            self.items[(m[2], m[1])] = bytes.fromhex(m[3]).decode("utf-8")
            return result(0)
        raise AssertionError(f"unexpected command {argv[:2]}")

    def open_url(self, request, timeout):
        url = request.full_url
        if url == TOKEN_URL:
            self.token_calls.append({"method": request.get_method(), "type": request.get_header("Content-type"),
                                     "body": json.loads(request.data), "timeout": timeout})
            if self.during_token:
                self.during_token()
            reply = self.answer() if callable(self.answer) else self.answer
            if isinstance(reply, BaseException):
                raise reply
            return io.BytesIO(json.dumps(reply).encode())
        self.used.append(request.get_header("Authorization"))
        if url == USAGE_URL:
            return io.BytesIO(json.dumps(USAGE).encode())
        if url == PROFILE_URL:
            return io.BytesIO(b'{"account": {"email": "b@example.test"}}')
        raise AssertionError("unexpected URL")

    def read(self, cache=None):
        return self.core.cached_reading("claude", self.acc, {} if cache is None else cache)

    def clear_cache(self):
        (self.home / "state" / "cache.json").unlink(missing_ok=True)

    def log(self, name):
        path = self.home / "state" / name
        return path.read_text(encoding="utf-8") if path.exists() else ""


def make_store(router, write_config, monkeypatch, mode, extra=""):
    from quota_router import config
    write_config(extra + ENDPOINTS + CONFIG)
    router.core.configure(config.load())
    from quota_router import platform_compat
    if mode == "keychain":
        monkeypatch.setattr(platform_compat, "system", lambda: "darwin")
    elif platform_compat.system() == "darwin":
        monkeypatch.setattr(platform_compat, "system", lambda: "linux")     # the file is the only store there
    return Store(router, mode, monkeypatch)


MODES = ["file"] + ([] if WINDOWS else ["keychain"])      # the keychain path takes the POSIX lock; Windows never has it


@pytest.fixture(params=MODES)
def store(request, router, write_config, monkeypatch):
    return make_store(router, write_config, monkeypatch, request.param)


@pytest.fixture
def file_store(router, write_config, monkeypatch):
    return make_store(router, write_config, monkeypatch, "file")


def test_an_expired_login_is_renewed_used_and_written_back(store):
    store.put(login())
    reading = store.read()
    assert len(store.token_calls) == 1
    call = store.token_calls[0]
    assert call["method"] == "POST" and call["type"] == "application/json" and call["timeout"] <= 4
    assert call["body"] == {"grant_type": "refresh_token", "refresh_token": OLD_REFRESH,
                            "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e", "scope": " ".join(SCOPES)}
    assert store.used and set(store.used) == {"Bearer " + NEW_ACCESS}               # usage and profile, new token only
    assert reading.get("live") is True and "error" not in reading
    assert reading["usage"]["seven_day"]["used"] == 2 and reading["email"] == "b@example.test"
    saved = store.stored()
    oauth = saved["claudeAiOauth"]
    assert oauth["accessToken"] == NEW_ACCESS and oauth["refreshToken"] == NEW_REFRESH       # the rotated one is kept
    assert abs(oauth["expiresAt"] / 1000 - (time.time() + 28800)) < 60
    assert (oauth["scopes"], oauth["subscriptionType"], oauth["rateLimitTier"]) == (SCOPES, "max", "default_claude_max_20x")
    assert saved["mcpOAuth"] == MCP and set(saved) == {"claudeAiOauth", "mcpOAuth"}
    assert store.core.load("cache.json", {})["claude-b"]["token_sig"] == store.core.fingerprint(NEW_ACCESS)
    if store.mode == "file":
        if not WINDOWS:
            assert stat.S_IMODE(os.stat(os.path.join(store.dir, ".credentials.json")).st_mode) == 0o600
        assert [n for n in os.listdir(store.dir) if n.endswith(".tmp")] == []
    else:
        assert ["security", "-i"] in store.argv                                    # the login went on stdin
        assert not any(NEW_ACCESS in word or NEW_REFRESH in word or NEW_ACCESS.encode().hex() in word
                       for argv in store.argv for word in argv)
    log = store.log("decisions.log")
    assert " | refresh | " in log and " | refresh | pick=claude-b | login renewed | cmd#-" in log
    assert not any(secret in log for secret in (OLD_ACCESS, OLD_REFRESH, NEW_ACCESS, NEW_REFRESH))
    # Claude Code's refresh locks were taken and given back.
    assert not os.path.exists(os.path.join(store.dir, ".oauth_refresh.lock"))
    assert not os.path.exists(os.path.realpath(store.dir) + ".lock")
    # the next reading uses the saved login and does not renew again.
    store.clear_cache()
    store.read()
    assert len(store.token_calls) == 1


def test_a_login_that_is_about_to_expire_is_renewed_too(file_store):
    file_store.put(login(expires_in=120))
    file_store.read()
    assert len(file_store.token_calls) == 1
    file_store.put(login(expires_in=3600))
    file_store.clear_cache()
    file_store.read()
    assert len(file_store.token_calls) == 1                     # an hour left: nothing to do


def test_an_answer_without_a_new_refresh_token_keeps_the_old_one(file_store):
    file_store.answer = answer(refresh_token=None, scope=None, refresh_token_expires_in=86400)
    file_store.put(login())
    file_store.read()
    oauth = file_store.stored()["claudeAiOauth"]
    assert oauth["accessToken"] == NEW_ACCESS and oauth["refreshToken"] == OLD_REFRESH and oauth["scopes"] == SCOPES
    assert abs(oauth["refreshTokenExpiresAt"] / 1000 - (time.time() + 86400)) < 60


@pytest.mark.skipif(WINDOWS, reason="the keychain path takes the POSIX lock")
def test_a_keychain_item_found_by_service_alone_is_written_back_to_the_same_item(router, write_config, monkeypatch):
    store = make_store(router, write_config, monkeypatch, "keychain")
    # Claude Code files the item under "claude-code-user" when the user name has unusual characters, so the query
    # with the user's name finds nothing and the one by service alone finds the login.
    store.put(login(), account="claude-code-user")
    store.read()
    assert len(store.token_calls) == 1
    assert store.stored("claude-code-user")["claudeAiOauth"]["accessToken"] == NEW_ACCESS
    assert list(store.items) == [(store.service, "claude-code-user")]


def test_no_renewal_while_claude_code_runs_on_that_directory(store):
    store.put(login())
    store.procs = {101: (1, "claude --dangerously-skip-permissions")}
    store.envs = {101: f"HOME=/x CLAUDE_CONFIG_DIR={store.dir} TERM=xterm"}
    reading = store.read()
    assert store.token_calls == [] and store.used == []
    assert reading["error"] == store.core.t("err.expired")
    assert store.stored() == login() | {"claudeAiOauth": store.stored()["claudeAiOauth"]}
    assert store.stored()["claudeAiOauth"]["accessToken"] == OLD_ACCESS
    # a process on another directory does not stop it.
    store.envs = {101: "HOME=/x CLAUDE_CONFIG_DIR=/somewhere/else"}
    store.clear_cache()
    store.read()
    assert len(store.token_calls) == 1


def test_a_process_classifier_sees_every_kind_of_claude_process(router):
    core = router.core
    for cmd in ("claude", "claude -p hi", "/Users/me/.local/bin/claude mcp serve", "claude daemon run",
                "node /usr/lib/node_modules/@anthropic-ai/claude-code/cli.js", "C:\\Users\\me\\bin\\claude.exe --resume x"):
        assert core.classify_claude_process(cmd) == "claude", cmd
    for cmd in ("/Applications/Claude.app/Contents/MacOS/Claude", "codex exec hi", "python claude.py",
                "node /x/node_modules/.bin/claude-in-mobile", "/bin/zsh -c claude"):
        assert core.classify_claude_process(cmd) is None, cmd


def test_someone_else_renewed_it_while_this_waited_for_the_lock(store, monkeypatch):
    store.put(login())
    fresh = login(access="sibling-access", refresh="sibling-refresh", expires_in=28800)
    real = store.pc.take_claude_refresh_lock

    def take(config_dir):
        store.put(fresh)                       # Claude Code finished its own renewal just before
        return real(config_dir)
    monkeypatch.setattr(store.pc, "take_claude_refresh_lock", take)
    reading = store.read()
    assert store.token_calls == []
    assert set(store.used) == {"Bearer sibling-access"} and reading.get("live") is True
    assert store.stored() == fresh


def test_claude_codes_own_refresh_lock_is_respected_and_a_stale_one_taken_over(file_store):
    file_store.put(login())
    lock = os.path.join(file_store.dir, ".oauth_refresh.lock")
    os.mkdir(lock)
    reading = file_store.read()
    assert file_store.token_calls == [] and reading["error"] == file_store.core.t("err.expired")
    assert os.path.isdir(lock)                                       # someone else's lock is left alone
    old = time.time() - 120
    os.utime(lock, (old, old))                                       # Claude Code renews it every 5 s; this one is dead
    file_store.clear_cache()
    file_store.read()
    assert len(file_store.token_calls) == 1 and not os.path.exists(lock)


@pytest.mark.parametrize("failure", [
    lambda: urllib.error.HTTPError(TOKEN_URL, 503, "Service Unavailable", {}, io.BytesIO(b"down")),
    lambda: urllib.error.URLError("no route"),
    lambda: {"token_type": "Bearer"},                                    # an answer without a token
    lambda: urllib.error.HTTPError(TOKEN_URL, 400, "Bad Request", {}, io.BytesIO(b'{"error": "invalid_scope"}')),
])
def test_a_failed_renewal_falls_back_to_the_expired_reading(file_store, failure):
    file_store.put(login())
    before = file_store.stored()
    file_store.answer = failure
    reading = file_store.read()
    assert len(file_store.token_calls) == 1 and file_store.used == []
    assert reading["error"] == file_store.core.t("err.expired")
    assert file_store.stored() == before
    assert " | login-refresh | " in file_store.log("hook-errors.log")
    assert " | refresh-failed | pick=claude-b | renewal failed | " in file_store.log("decisions.log")
    # the next readings wait out the cooldown instead of asking again on every dispatch.
    file_store.read()
    assert len(file_store.token_calls) == 1


def test_a_refused_refresh_token_asks_for_claude_login(file_store):
    file_store.put(login())
    file_store.answer = dead_answer
    reading = file_store.read()
    assert reading["error"] == file_store.core.t("err.login_dead") != file_store.core.t("err.expired")
    assert "claude login" in file_store.core.t("err.login_dead")
    assert " | refresh-dead | pick=claude-b | refresh token refused; run claude login there | " in file_store.log("decisions.log")
    assert file_store.stored()["claudeAiOauth"]["refreshToken"] == OLD_REFRESH
    # after the cooldown the same refresh token is not sent again; a new login clears the mark.
    cache = file_store.core.load("cache.json", {})
    cache["claude-b"]["refresh_retry_at"] = 0
    file_store.core.save("cache.json", cache)
    assert file_store.read()["error"] == file_store.core.t("err.login_dead")
    assert len(file_store.token_calls) == 1
    file_store.put(login(access="relogin-access", refresh="relogin-refresh"))
    file_store.answer = answer()
    file_store.read()
    assert len(file_store.token_calls) == 2 and file_store.token_calls[1]["body"]["refresh_token"] == "relogin-refresh"


def test_an_expired_refresh_token_is_not_sent(file_store):
    file_store.put(login(refreshTokenExpiresAt=int((time.time() - 60) * 1000)))
    reading = file_store.read()
    assert file_store.token_calls == [] and reading["error"] == file_store.core.t("err.login_dead")


def test_qr_status_says_a_dead_login_needs_claude_login(file_store, monkeypatch, capsys):
    from quota_router import i18n
    core = file_store.core
    monkeypatch.setattr(core, "claude_reading", lambda acc, cache, force=False: core.cached_reading("claude", acc, cache, force))
    file_store.put(login())
    file_store.answer = dead_answer
    core.status()
    out = capsys.readouterr().out
    assert core.t("err.login_dead") in out and core.t("err.expired") not in out
    assert "claude login" in i18n.TABLES["zh-TW"]["err.login_dead"]


@pytest.mark.parametrize("how", ["config", "env"])
def test_renewal_can_be_turned_off(router, write_config, monkeypatch, how):
    store = make_store(router, write_config, monkeypatch, "file",
                       extra="[families.claude]\nrefresh_logins = false\n" if how == "config" else "")
    if how == "env":
        monkeypatch.setenv("QR_NO_REFRESH", "1")
    store.put(login())
    reading = store.read()
    assert store.token_calls == [] and reading["error"] == store.core.t("err.expired")
    assert store.stored()["claudeAiOauth"]["accessToken"] == OLD_ACCESS


def test_no_renewal_without_enough_time_left(file_store):
    file_store.put(login())
    core = file_store.core
    with core.budget(time.monotonic() + 3):
        reading = file_store.read()
    assert file_store.token_calls == [] and reading["error"] == core.t("err.expired")
    with core.budget(time.monotonic() + 8):
        file_store.clear_cache()
        file_store.read()
    assert len(file_store.token_calls) == 1 and file_store.token_calls[0]["timeout"] <= 4


def test_a_failed_write_after_a_renewal_is_loud(store, monkeypatch):
    store.put(login())
    if store.mode == "file":
        real = store.pc.replace

        def refuse(source, target):
            if target.endswith(".credentials.json"):
                raise PermissionError(13, "denied")
            return real(source, target)
        monkeypatch.setattr(store.pc, "replace", refuse)
    else:
        store.fail_write = True
    reading = store.read()
    assert len(store.token_calls) == 1
    assert reading["error"] == store.core.t("err.refresh_unsaved")
    assert store.core.load("cache.json", {})["claude-b"]["error"] == store.core.t("err.refresh_unsaved")
    assert " | login-refresh-save | " in store.log("hook-errors.log")
    assert " | refresh-unsaved | pick=claude-b | renewed but could not be saved | " in store.log("decisions.log")
    assert store.stored()["claudeAiOauth"]["accessToken"] == OLD_ACCESS                      # nothing half-written
    if store.mode == "file":
        assert [n for n in os.listdir(store.dir) if n.endswith(".tmp")] == []


def test_a_login_changed_during_the_renewal_is_not_overwritten(file_store):
    file_store.put(login())
    other = login(access="other-access", refresh="other-refresh")         # someone else wrote a login meanwhile
    file_store.during_token = lambda: file_store.put(other)
    file_store.read()
    assert file_store.stored() == other
    assert " | refresh-unsaved | pick=claude-b | " in file_store.log("decisions.log")
    assert " | login-refresh-save | " in file_store.log("hook-errors.log")


def test_only_a_complete_login_is_ever_written(home):
    from quota_router import platform_compat as pc
    with pytest.raises(ValueError):
        pc.save_claude_login(("file", str(home / ".credentials.json")), '{"mcpOAuth": {}}', None)
    with pytest.raises(ValueError):
        pc.save_claude_login(("keychain", "svc", "me"), "{", None)
    assert not (home / ".credentials.json").exists()


def test_the_keychain_write_matches_claude_codes(home):
    """`add-generic-password -U` on the same item, hex-encoded on stdin; argv only above Claude Code's stdin limit."""
    from quota_router import platform_compat as pc
    calls = []

    def run(argv, input=None):
        calls.append((argv, input))
        return result(0)
    text = json.dumps(login())
    pc.save_claude_login(("keychain", "Claude Code-credentials-1234abcd", "me"), text, run)
    assert calls == [(["security", "-i"], f'add-generic-password -U -a "me" -s "Claude Code-credentials-1234abcd" '
                                          f'-X "{text.encode().hex()}"\n')]
    calls.clear()
    big = json.dumps(dict(login(), padding="x" * 3000))
    pc.save_claude_login(("keychain", "svc", "me"), big, run)
    assert calls == [(["security", "add-generic-password", "-U", "-a", "me", "-s", "svc", "-X", big.encode().hex()], None)]
    with pytest.raises(OSError):
        pc.save_claude_login(("keychain", "svc", "me"), text, lambda argv, input=None: result(36))
    with pytest.raises(OSError):
        pc.save_claude_login(("keychain", "svc", 'me" -s "other'), text, run)


def test_claude_login_reports_where_the_login_came_from(home, monkeypatch):
    from quota_router import platform_compat as pc
    monkeypatch.setattr(pc, "system", lambda: "darwin")
    monkeypatch.setattr(pc, "login_names", lambda: ["me"])
    text = json.dumps(login())
    service = pc.keychain_service(None)

    def run(argv, input=None):
        return result(0, text + "\n") if "-a" in argv else result(44)
    assert pc.claude_login(None, run) == (text + "\n", ("keychain", service, "me"))
    assert pc.claude_login(None, lambda argv: result(0, text) if "-a" not in argv else result(44)) == \
        (text, ("keychain", service, None))
    os.makedirs(home / ".claude")
    (home / ".claude" / ".credentials.json").write_text(text, encoding="utf-8")
    assert pc.claude_login(None, lambda argv: result(44)) == (text, ("file", str(home / ".claude" / ".credentials.json")))
    assert pc.claude_credentials(None, lambda argv: result(44)) == text
