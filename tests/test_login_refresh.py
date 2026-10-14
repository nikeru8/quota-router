"""quota-router renews an expired Claude login with its refresh token and writes it back where Claude Code keeps it.

A reading (the hook, qr status / pick / check / launch) never contacts the token endpoint: it starts one background
process (`renew-login <key>`), and that process does the renewal outside every time budget. Here the background
process is run in-process (Store.spawn), right where the reading would have started it.

Nothing here reaches the network, the real keychain or the real process table: the token and usage endpoints are a
fake `open_url`, `security` is a fake runner over a dict, and the process list is a dict.
"""
import http.server
import io
import json
import os
import re
import signal
import stat
import sys
import threading
import time
import types
import urllib.error

import pytest

from conftest import CONFIG, WINDOWS
from quota_router.platform_compat import process_environment as REAL_PROCESS_ENVIRONMENT

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
    """The shape of the real answer seen on 2026-10-13 (token_uuid, organization and account are not stored)."""
    data = {"token_type": "Bearer", "access_token": NEW_ACCESS, "expires_in": 28800, "refresh_token": NEW_REFRESH,
            "refresh_token_expires_in": 1862778, "scope": " ".join(SCOPES), "token_uuid": "u-1",
            "organization": {"uuid": "o-1"}, "account": {"uuid": "a-1", "email_address": "b@example.test"}}
    data.update(override)
    return {k: v for k, v in data.items() if v is not None}


def http_error(code, body):
    return urllib.error.HTTPError(TOKEN_URL, code, "Error", {}, io.BytesIO(json.dumps(body).encode()))


def dead_answer(code=400):
    return lambda: http_error(code, {"error": "invalid_grant", "error_description": "Refresh token not found or invalid"})


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
        self.process_reads = 0
        self.fail_write = 0              # this many keychain writes fail
        self.starts, self.outcomes = [], []
        self.child = True                # run the background renewal right away, in this process
        monkeypatch.setattr(self.core, "run_local", self.run)
        monkeypatch.setattr(self.core, "open_url", self.open_url)
        monkeypatch.setattr(self.core, "spawn_renewal", self.spawn)
        monkeypatch.setattr(self.core, "REFRESH_WRITE_PAUSES", (0, 0, 0))
        monkeypatch.setattr(self.core, "REFRESH_READBACK_PAUSES", (0, 0, 0))
        monkeypatch.setattr(self.core, "RENEWALS", [])
        monkeypatch.setattr(platform_compat, "read_processes", self.read_processes)
        if not WINDOWS:
            # Windows keeps the real one, which never reads another process's environment (it returns None).
            monkeypatch.setattr(platform_compat, "process_environment", lambda pid, command, run: self.envs.get(pid))
        monkeypatch.setattr(platform_compat, "login_names", lambda: ["tester"])
        monkeypatch.setattr(self.core, "claude_local_cache", lambda acc: None)

    @property
    def service(self):
        return self.pc.keychain_service(self.dir)

    def read_processes(self, run):
        self.process_reads += 1
        return None if self.procs is None else dict(self.procs)

    def spawn(self, acc):
        self.starts.append(acc["key"])
        if self.child:
            with self.core.budget(None):
                self.outcomes.append(self.core.renew_login(acc))
        return None

    def run_on(self, cmd, directory):
        """One Claude Code process `cmd` using `directory`, as this platform can tell: from its environment, or on
        Windows from `CLAUDE_CONFIG_DIR=…` on its parent's command line."""
        if WINDOWS:
            self.procs = {100: (1, f'bash -c "CLAUDE_CONFIG_DIR={directory} claude"'), 101: (100, cmd)}
        else:
            self.procs = {101: (1, cmd)}
            self.envs = {101: f"HOME=/x CLAUDE_CONFIG_DIR={directory} TERM=xterm"}

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
                self.fail_write -= 1
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
                                     "agent": request.get_header("User-agent"), "body": json.loads(request.data),
                                     "timeout": timeout})
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

    def again(self):
        """Forget the last reading and the start marker, as if the minimum gap between two renewals had passed."""
        self.clear_cache()
        marker = self.home / "state" / "refresh-claude-b.start"
        marker.unlink(missing_ok=True)

    def clear_cache(self):
        (self.home / "state" / "cache.json").unlink(missing_ok=True)

    def cache(self):
        return self.core.load("cache.json", {}).get("claude-b", {})

    def end_cooldown(self):
        cache = self.core.load("cache.json", {})
        cache["claude-b"]["refresh_retry_at"] = 0
        self.core.save("cache.json", cache)
        (self.home / "state" / "refresh-claude-b.start").unlink(missing_ok=True)

    def log(self, name):
        path = self.home / "state" / name
        return path.read_text(encoding="utf-8") if path.exists() else ""


def make_store(router, write_config, monkeypatch, mode, extra=""):
    from quota_router import config
    if "refresh_logins" not in extra:
        extra += "[families.claude]\nrefresh_logins = true\n"   # off by default; these tests are about renewing
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


def lock_paths(store):
    return [os.path.join(store.dir, ".oauth_refresh.lock"), os.path.realpath(store.dir) + ".lock",
            os.path.join(store.dir, ".storage-write.lock")]


# ---- the normal case ----------------------------------------------------------------------------------------------
def test_an_expired_login_is_renewed_in_the_background_and_written_back(store):
    store.put(login())
    reading = store.read()
    # this reading started the renewal and queried nothing with the expired token. (The renewal, run here before
    # the reading ended, changed the token under it, so the reading keeps what was cached before.)
    assert store.starts == ["claude-b"] and store.outcomes == ["renewed"]
    assert store.used == [] and not reading.get("live")
    assert len(store.token_calls) == 1
    call = store.token_calls[0]
    assert call["method"] == "POST" and call["type"] == "application/json" and call["timeout"] == 30
    assert call["agent"] == f"quota-router/{store.core.__version__}"            # Cloudflare refuses Python-urllib
    assert call["body"] == {"grant_type": "refresh_token", "refresh_token": OLD_REFRESH,
                            "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e", "scope": " ".join(SCOPES)}
    saved = store.stored()
    oauth = saved["claudeAiOauth"]
    assert oauth["accessToken"] == NEW_ACCESS and oauth["refreshToken"] == NEW_REFRESH       # the rotated one is kept
    assert abs(oauth["expiresAt"] / 1000 - (time.time() + 28800)) < 60
    assert abs(oauth["refreshTokenExpiresAt"] / 1000 - (time.time() + 1862778)) < 60
    assert (oauth["scopes"], oauth["subscriptionType"], oauth["rateLimitTier"]) == (SCOPES, "max", "default_claude_max_20x")
    assert saved["mcpOAuth"] == MCP and set(saved) == {"claudeAiOauth", "mcpOAuth"}
    assert set(oauth) == {"accessToken", "refreshToken", "expiresAt", "refreshTokenExpiresAt", "scopes",
                          "subscriptionType", "rateLimitTier"}                    # nothing of token_uuid / account
    if store.mode == "file":
        if not WINDOWS:
            assert stat.S_IMODE(os.stat(os.path.join(store.dir, ".credentials.json")).st_mode) == 0o600
        assert [n for n in os.listdir(store.dir) if n.endswith(".tmp")] == []
    else:
        assert ["security", "-i"] in store.argv                                    # the login went on stdin
        assert not any(NEW_ACCESS in word or NEW_REFRESH in word or NEW_ACCESS.encode().hex() in word
                       for argv in store.argv for word in argv)
    log = store.log("decisions.log")
    assert " | refresh-started | pick=claude-b | renewal started in the background | " in log
    assert " | refresh | pick=claude-b | login renewed | cmd#-" in log
    assert not any(secret in log for secret in (OLD_ACCESS, OLD_REFRESH, NEW_ACCESS, NEW_REFRESH))
    assert not any(os.path.exists(path) for path in lock_paths(store))          # Claude Code's locks were given back
    # the same login with a new token: its cache entry follows it, and the next reading is live with the new token.
    assert store.cache()["token_sig"] == store.core.fingerprint(NEW_ACCESS)
    reading = store.read()
    assert reading.get("live") is True and "error" not in reading
    assert store.used and set(store.used) == {"Bearer " + NEW_ACCESS}
    assert reading["usage"]["seven_day"]["used"] == 2 and reading["email"] == "b@example.test"
    assert len(store.token_calls) == 1 and store.starts == ["claude-b"]


def test_a_reading_never_contacts_the_token_endpoint_itself(file_store):
    file_store.child = False                 # the background process has not run yet
    file_store.put(login())
    with file_store.core.budget(time.monotonic() + 8):
        reading = file_store.read()
    assert file_store.starts == ["claude-b"] and file_store.token_calls == []
    assert reading["error"] == file_store.core.t("err.expired")
    assert (file_store.home / "state" / "refresh-claude-b.start").exists()
    # one renewal per account at a time, and a minimum gap between two starts.
    file_store.clear_cache()
    file_store.read()
    assert file_store.starts == ["claude-b"]
    marker = file_store.home / "state" / "refresh-claude-b.start"
    old = time.time() - file_store.core.REFRESH_GAP_SEC - 1
    os.utime(marker, (old, old))
    file_store.clear_cache()
    file_store.read()
    assert file_store.starts == ["claude-b", "claude-b"]


def test_a_login_that_is_about_to_expire_is_renewed_while_it_is_still_used(file_store):
    file_store.child = False
    file_store.put(login(expires_in=120))
    reading = file_store.read()
    assert file_store.starts == ["claude-b"]
    assert reading.get("live") is True and set(file_store.used) == {"Bearer " + OLD_ACCESS}
    file_store.put(login(expires_in=3600))
    file_store.again()
    file_store.read()
    assert file_store.starts == ["claude-b"]                     # an hour left: nothing to do


def test_no_start_without_two_seconds_of_budget_left(file_store):
    file_store.child = False
    file_store.put(login())
    with file_store.core.budget(time.monotonic() + 1.5):
        reading = file_store.read()
    assert file_store.starts == [] and reading["error"] == file_store.core.t("err.expired")
    file_store.again()
    with file_store.core.budget(time.monotonic() + 2.5):
        file_store.read()
    assert file_store.starts == ["claude-b"]


def test_spawn_renewal_runs_renew_login_detached_with_its_own_log(router, monkeypatch):
    core = router.core
    calls = []

    class Process:
        pass
    monkeypatch.setattr(core.platform_compat, "spawn_detached",
                        lambda argv, env, stdout, stderr: calls.append((argv, env, stdout.name, stderr.name)) or Process())
    acc = next(a for a in core.CLAUDE_ACCOUNTS if a["key"] == "claude-b")
    assert isinstance(core.spawn_renewal(acc), Process)
    argv, env, out, err = calls[0]
    assert argv == [sys.executable, "-I", "-X", "utf8", os.path.join(os.path.dirname(core.__file__), "__main__.py"),
                    "renew-login", "claude-b"]
    assert env["QR_STATE"] == os.environ["QR_STATE"] and env["QR_CONFIG"] == os.environ["QR_CONFIG"]
    assert out == err == os.path.join(core.STATE, "refresh-claude-b.log")


def test_qr_renew_login_runs_the_renewal_and_writes_one_line(file_store, monkeypatch, capsys):
    from quota_router import cli
    file_store.put(login())
    with pytest.raises(SystemExit) as ended:
        cli.main(["renew-login", "claude-b"])
    assert ended.value.code == 0
    out = capsys.readouterr()
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d renewed\n", out.out) and out.err == ""
    assert file_store.stored()["claudeAiOauth"]["refreshToken"] == NEW_REFRESH
    assert file_store.core.renewal_main(["no-such-account"]) == 2


def test_an_unexpected_crash_in_the_background_is_one_line_without_a_traceback(file_store, monkeypatch, capsys):
    file_store.put(login())

    def boom(acc):
        raise RuntimeError(f"secret {NEW_REFRESH}")
    monkeypatch.setattr(file_store.core, "renew_login", boom)
    assert file_store.core.renewal_main(["claude-b"]) == 0
    out = capsys.readouterr()
    assert out.out.rstrip().endswith(" failed") and NEW_REFRESH not in out.out + out.err
    assert " | login-refresh | RuntimeError | " in file_store.log("hook-errors.log")


def test_qr_status_refresh_on_a_terminal_waits_for_the_renewal_and_redraws(file_store, monkeypatch, capsys):
    core = file_store.core
    monkeypatch.setattr(core, "claude_reading", lambda acc, cache, force=False: core.cached_reading("claude", acc, cache, force))
    monkeypatch.setattr(core, "interactive", lambda: True)
    waited = []

    class Process:
        def wait(self, timeout):
            waited.append(timeout)
            with core.budget(None):
                core.renew_login(file_store.acc)      # the background renewal finishes while status waits
    file_store.child = False
    real_spawn = file_store.spawn
    monkeypatch.setattr(core, "spawn_renewal", lambda acc: real_spawn(acc) or Process())
    file_store.put(login())
    core.status(force=True)
    out = capsys.readouterr()
    assert waited and 30 <= waited[0] <= 35
    assert "claude-b" in out.err and core.t("err.expired") not in out.out
    assert "Bearer " + NEW_ACCESS in file_store.used


def test_qr_status_without_a_terminal_does_not_wait(file_store, monkeypatch, capsys):
    core = file_store.core
    monkeypatch.setattr(core, "claude_reading", lambda acc, cache, force=False: core.cached_reading("claude", acc, cache, force))
    monkeypatch.setattr(core, "interactive", lambda: False)

    class Process:
        def wait(self, timeout):
            raise AssertionError("waited")
    file_store.child = False
    monkeypatch.setattr(core, "spawn_renewal", lambda acc: file_store.spawn(acc) or Process())
    file_store.put(login())
    core.status(force=True)
    assert core.t("err.expired") in capsys.readouterr().out


# ---- answers ------------------------------------------------------------------------------------------------------
def test_an_answer_without_a_new_refresh_token_keeps_the_old_one(file_store):
    file_store.answer = answer(refresh_token=None, scope=None, refresh_token_expires_in=86400)
    file_store.put(login())
    file_store.read()
    oauth = file_store.stored()["claudeAiOauth"]
    assert oauth["accessToken"] == NEW_ACCESS and oauth["refreshToken"] == OLD_REFRESH and oauth["scopes"] == SCOPES
    assert abs(oauth["refreshTokenExpiresAt"] / 1000 - (time.time() + 86400)) < 60


def test_an_odd_expiry_still_stores_the_new_tokens_with_a_short_one(file_store):
    file_store.answer = answer(expires_in="soon")
    file_store.put(login())
    file_store.read()
    oauth = file_store.stored()["claudeAiOauth"]
    assert (oauth["accessToken"], oauth["refreshToken"]) == (NEW_ACCESS, NEW_REFRESH)
    assert abs(oauth["expiresAt"] / 1000 - (time.time() + 3600)) < 60
    assert file_store.outcomes == ["renewed"]


@pytest.mark.parametrize("odd", [{"access_token": None}, {"scope": "user:profile"}, {"access_token": 7}])
def test_an_answer_that_is_not_a_login_still_keeps_its_new_refresh_token(file_store, odd):
    """The refresh token that was sent is dead once the server answered; its successor is saved, whatever else the
    answer lacks. An answer without inference scope is not taken as the login, as Claude Code would not."""
    file_store.answer = answer(**odd)
    file_store.put(login())
    reading = file_store.read()
    oauth = file_store.stored()["claudeAiOauth"]
    assert oauth["refreshToken"] == NEW_REFRESH
    assert oauth["accessToken"] == OLD_ACCESS and oauth["scopes"] == SCOPES
    assert file_store.outcomes == ["failed"] and reading["error"] == file_store.core.t("err.expired")
    assert " | refresh-failed | pick=claude-b | odd answer; new refresh token kept | " in file_store.log("decisions.log")
    assert file_store.cache()["refresh_retry_at"] > time.time() + 500
    # the next attempt, after the cooldown, sends the new refresh token.
    file_store.answer = answer(access_token="third-access", refresh_token="third-refresh")
    file_store.end_cooldown()
    file_store.read()
    assert file_store.token_calls[-1]["body"]["refresh_token"] == NEW_REFRESH
    assert file_store.stored()["claudeAiOauth"]["accessToken"] == "third-access"


def test_an_answer_without_anything_new_is_a_failure(file_store):
    file_store.answer = {"token_type": "Bearer"}
    file_store.put(login())
    before = file_store.stored()
    file_store.read()
    assert file_store.stored() == before and file_store.outcomes == ["failed"]
    assert " | login-refresh | ValueError | " in file_store.log("hook-errors.log")


def test_the_client_id_stored_with_the_login_is_used(file_store):
    file_store.put(login(clientId="custom-client-id"))
    file_store.read()
    assert file_store.token_calls[0]["body"]["client_id"] == "custom-client-id"


# ---- what keeps a renewal from starting ---------------------------------------------------------------------------
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


@pytest.mark.parametrize("cmd", ["claude --dangerously-skip-permissions", "claude -p summarise", "claude mcp serve",
                                 "node /usr/lib/node_modules/@anthropic-ai/claude-code/cli.js -p x"])
def test_no_renewal_while_any_claude_code_process_runs_on_that_directory(store, cmd):
    store.put(login())
    store.run_on(cmd, store.dir)
    reading = store.read()
    assert store.starts == [] and store.token_calls == [] and store.used == []
    assert reading["error"] == store.core.t("err.expired")
    assert store.stored()["claudeAiOauth"]["accessToken"] == OLD_ACCESS
    # "busy" waits a minute: the process list is not read again on every dispatch.
    cooldown = store.cache()["refresh_retry_at"] - time.time()
    assert store.cache()["refresh_failed"] == "busy" and 50 < cooldown <= 60
    reads = store.process_reads
    store.read()
    assert store.process_reads == reads
    # a process on another directory does not stop it.
    store.run_on(cmd, "C:/somewhere/else" if WINDOWS else "/somewhere/else")
    store.end_cooldown()
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


def test_an_unreadable_process_list_counts_as_busy(file_store):
    file_store.put(login())
    file_store.procs = None
    file_store.read()
    assert file_store.starts == [] and file_store.cache()["refresh_failed"] == "busy"


def test_a_claude_process_whose_environment_cannot_be_read_counts_as_busy(file_store):
    file_store.put(login())
    file_store.procs = {101: (1, "claude")}
    file_store.envs = {}                                # it ended between the two `ps` calls, or did not answer
    file_store.read()
    assert file_store.starts == []


@pytest.mark.parametrize("command, busy", [
    ("C:\\Users\\me\\.local\\bin\\claude.exe --resume abc", True),        # directory unknown: fails closed
    ('C:\\Windows\\System32\\cmd.exe /c "set CLAUDE_CONFIG_DIR=C:/x"', False),   # not a Claude process
])
def test_windows_counts_a_claude_process_of_unknown_directory_as_busy(file_store, monkeypatch, command, busy):
    """Real Windows: process_environment() is always None, so only a directory written on a command line counts."""
    from quota_router import platform_compat as pc
    core = file_store.core
    real = core.login_busy
    monkeypatch.setattr(pc, "process_environment", REAL_PROCESS_ENVIRONMENT)

    def as_windows(acc):
        system = pc.system
        pc.system = lambda: "windows"
        try:
            return real(acc)
        finally:
            pc.system = system
    monkeypatch.setattr(core, "login_busy", as_windows)
    file_store.put(login())
    file_store.procs = {101: (1, command)}
    file_store.read()
    assert (file_store.starts == []) is busy


def test_windows_attributes_a_directory_written_on_a_parent_command_line(file_store, monkeypatch):
    from quota_router import platform_compat as pc
    core = file_store.core
    real = core.login_busy
    monkeypatch.setattr(pc, "process_environment", REAL_PROCESS_ENVIRONMENT)

    def as_windows(acc):
        system = pc.system
        pc.system = lambda: "windows"
        try:
            return real(acc)
        finally:
            pc.system = system
    monkeypatch.setattr(core, "login_busy", as_windows)
    file_store.put(login())
    file_store.procs = {100: (1, 'bash -c "CLAUDE_CONFIG_DIR=C:/elsewhere claude --resume x"'),
                        101: (100, "C:\\Users\\me\\.local\\bin\\claude.exe --resume x")}
    file_store.read()
    assert file_store.starts == ["claude-b"]
    file_store.procs[100] = (1, f'bash -c "CLAUDE_CONFIG_DIR={file_store.dir} claude --resume x"')
    file_store.put(login())
    file_store.again()
    file_store.read()
    assert file_store.starts == ["claude-b"]


def test_the_background_process_checks_for_claude_code_again(file_store, monkeypatch):
    """A session may start between the reading and the renewal."""
    file_store.put(login())
    real = file_store.spawn

    def spawn(acc):
        file_store.run_on("claude", file_store.dir)
        return real(acc)
    monkeypatch.setattr(file_store.core, "spawn_renewal", spawn)
    file_store.read()
    assert file_store.outcomes == ["busy"] and file_store.token_calls == []
    assert " | refresh-busy | pick=claude-b | Claude Code may be using that directory | " in file_store.log("decisions.log")


def test_another_renewal_of_the_same_account_holding_its_lock_stops_this_one(file_store):
    file_store.put(login())
    core = file_store.core
    with core.state_lock("refresh-claude-b") as held:
        assert held
        file_store.read()
    assert file_store.outcomes == ["busy"] and file_store.token_calls == []
    assert " | refresh-busy | pick=claude-b | another renewal is running | " in file_store.log("decisions.log")


@pytest.mark.parametrize("which", [0, 1, 2])
def test_each_of_claude_codes_locks_is_respected_and_a_stale_one_taken_over(file_store, which):
    """0: .oauth_refresh.lock, 1: the legacy <dir>.lock, 2: .storage-write.lock (stale after 15 s, not 60 s)."""
    file_store.put(login())
    lock = lock_paths(file_store)[which]
    os.mkdir(lock)
    file_store.read()
    assert file_store.token_calls == [] and file_store.outcomes == ["busy"]
    assert os.path.isdir(lock)                                       # someone else's lock is left alone
    assert " | refresh-busy | pick=claude-b | Claude Code holds its lock | " in file_store.log("decisions.log")
    assert all(not os.path.exists(p) for p in lock_paths(file_store) if p != lock)    # none of the others is kept
    old = time.time() - (20 if which == 2 else 50)
    os.utime(lock, (old, old))
    file_store.end_cooldown()
    file_store.read()
    assert file_store.token_calls == [] if which != 2 else len(file_store.token_calls) == 1
    old = time.time() - 120                                          # Claude Code renews it every few seconds; this one is dead
    os.utime(lock, (old, old)) if os.path.exists(lock) else None
    file_store.put(login()) if which == 2 else None
    file_store.end_cooldown()
    file_store.read()
    assert len(file_store.token_calls) == (2 if which == 2 else 1) and not os.path.exists(lock)


def test_the_locks_are_kept_fresh_while_the_answer_takes_its_time(file_store, monkeypatch):
    monkeypatch.setattr(file_store.pc, "CLAUDE_LOCK_BEAT_SEC", 0.05)
    file_store.put(login())
    seen = {}

    def slow():
        old = time.time() - 600
        for path in lock_paths(file_store):
            os.utime(path, (old, old))
        time.sleep(0.3)
        seen.update({path: os.stat(path).st_mtime for path in lock_paths(file_store)})
    file_store.during_token = slow
    file_store.read()
    assert len(seen) == 3 and all(mtime > time.time() - 60 for mtime in seen.values())


def test_someone_else_renewed_it_while_this_waited_for_the_lock(file_store, monkeypatch):
    file_store.put(login())
    fresh = login(access="sibling-access", refresh="sibling-refresh", expires_in=28800)
    real = file_store.pc.take_claude_refresh_lock

    def take(config_dir):
        file_store.put(fresh)                       # Claude Code finished its own renewal just before
        return real(config_dir)
    monkeypatch.setattr(file_store.pc, "take_claude_refresh_lock", take)
    file_store.read()
    assert file_store.token_calls == [] and file_store.outcomes == ["adopted"]
    assert file_store.stored() == fresh
    reading = file_store.read()
    assert set(file_store.used) == {"Bearer sibling-access"} and reading.get("live") is True


@pytest.mark.parametrize("change", [
    {"scopes": ["user:profile"]},                      # not a Claude.ai login: Claude Code does not renew it either
    {"scopes": []},
    {"scopes": "user:inference"},
    {"refreshToken": ""},
])
def test_a_login_that_is_not_renewable_is_left_alone(file_store, change):
    file_store.put(login(**change))
    file_store.read()
    assert file_store.starts == [] and file_store.cache()["refresh_failed"] == "none"
    assert 50 < file_store.cache()["refresh_retry_at"] - time.time() <= 60


@pytest.mark.parametrize("how", ["config", "env"])
def test_renewal_can_be_turned_off(router, write_config, monkeypatch, how):
    store = make_store(router, write_config, monkeypatch, "file",
                       extra="[families.claude]\nrefresh_logins = false\n" if how == "config" else "")
    if how == "env":
        monkeypatch.setenv("QR_NO_REFRESH", "1")
    store.put(login())
    reading = store.read()
    assert store.starts == [] and reading["error"] == store.core.t("err.expired")
    # a background process started before the switch was turned off does nothing either.
    assert store.core.renew_login(store.acc) == "off"
    assert store.token_calls == [] and store.stored()["claudeAiOauth"]["accessToken"] == OLD_ACCESS


@pytest.mark.skipif(WINDOWS, reason="Windows has no keychain")
def test_a_login_from_the_keychain_never_creates_a_credentials_file(router, write_config, monkeypatch):
    store = make_store(router, write_config, monkeypatch, "keychain")
    store.put(login())
    store.read()
    assert not os.path.exists(os.path.join(store.dir, ".credentials.json"))


def test_a_missing_credentials_file_is_never_created(home):
    from quota_router import platform_compat as pc
    path = str(home / ".claude-x" / ".credentials.json")
    os.makedirs(home / ".claude-x")
    with pytest.raises(OSError):
        pc.save_claude_login(("file", path), json.dumps(login()), None)
    assert not os.path.exists(path)


def test_the_windows_credential_manager_leaves_the_file_alone(file_store, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_FORCE_WINDOWS_CREDMAN", "1")
    monkeypatch.setattr(file_store.pc, "system", lambda: "windows")
    file_store.put(login())
    oauth = file_store.stored()["claudeAiOauth"]
    assert file_store.core.renewal_blocker(oauth, {}) == "none"


# ---- failures -----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("failure, kind", [
    (lambda: http_error(503, {"error": "down"}), "HTTPError-503"),
    (lambda: http_error(403, {"error": "cloudflare 1010"}), "HTTPError-403"),
    (lambda: urllib.error.URLError("no route"), "URLError"),
    (lambda: http_error(400, {"error": "invalid_scope"}), "HTTPError-400"),
    (lambda: TimeoutError("timed out"), "TimeoutError"),
])
def test_a_failed_renewal_falls_back_to_the_expired_reading(file_store, failure, kind):
    file_store.put(login())
    before = file_store.stored()
    file_store.answer = failure
    reading = file_store.read()
    assert len(file_store.token_calls) == 1 and file_store.used == []
    assert reading["error"] == file_store.core.t("err.expired")
    assert file_store.stored() == before
    assert f" | login-refresh | {kind} | " in file_store.log("hook-errors.log")
    assert " | refresh-failed | pick=claude-b | renewal failed" in file_store.log("decisions.log")
    assert file_store.cache()["refresh_retry_at"] > time.time() + 590
    # the next readings wait out the cooldown instead of asking again on every dispatch.
    (file_store.home / "state" / "refresh-claude-b.start").unlink()
    file_store.read()
    assert len(file_store.token_calls) == 1


def test_a_rate_limited_answer_is_a_failure_with_the_normal_cooldown_and_not_retried(file_store):
    file_store.put(login())
    file_store.answer = lambda: http_error(429, {"error": {"type": "rate_limit_error", "message": "slow down"}})
    reading = file_store.read()
    assert len(file_store.token_calls) == 1 and file_store.outcomes == ["failed"]
    assert reading["error"] == file_store.core.t("err.expired") != file_store.core.t("err.login_dead")
    cache = file_store.cache()
    assert cache["refresh_failed"] == "failed" and "refresh_dead" not in cache
    assert cache["refresh_retry_at"] > time.time() + 590
    assert " | login-refresh | HTTPError-429 | " in file_store.log("hook-errors.log")
    assert " | refresh-failed | pick=claude-b | renewal failed, HTTP 429 | " in file_store.log("decisions.log")
    # after the cooldown the same refresh token is tried again: a 429 says nothing about the token.
    file_store.answer = answer()
    file_store.end_cooldown()
    file_store.read()
    assert len(file_store.token_calls) == 2 and file_store.stored()["claudeAiOauth"]["accessToken"] == NEW_ACCESS


@pytest.mark.parametrize("code", [400, 401])
def test_a_refused_refresh_token_asks_for_claude_login(file_store, code):
    file_store.put(login())
    file_store.answer = dead_answer(code)
    reading = file_store.read()
    assert file_store.outcomes == ["dead"]
    assert file_store.read()["error"] == file_store.core.t("err.login_dead") != file_store.core.t("err.expired")
    assert reading["error"] == file_store.core.t("err.expired")       # the reading that started it did not know yet
    assert "claude login" in file_store.core.t("err.login_dead")
    assert " | refresh-dead | pick=claude-b | refresh token refused; run claude login there | " in file_store.log("decisions.log")
    assert f" | login-refresh | HTTPError-{code} | " in file_store.log("hook-errors.log")
    assert file_store.stored()["claudeAiOauth"]["refreshToken"] == OLD_REFRESH
    # after the cooldown the same refresh token is not sent again; a new login clears the mark.
    file_store.end_cooldown()
    assert file_store.read()["error"] == file_store.core.t("err.login_dead")
    assert len(file_store.token_calls) == 1
    file_store.put(login(access="relogin-access", refresh="relogin-refresh"))
    file_store.answer = answer()
    file_store.end_cooldown()
    file_store.read()
    assert len(file_store.token_calls) == 2 and file_store.token_calls[1]["body"]["refresh_token"] == "relogin-refresh"


def test_a_new_login_drops_the_dead_mark(file_store):
    file_store.put(login())
    file_store.answer = dead_answer()
    file_store.read()
    assert file_store.cache()["refresh_dead"]
    file_store.child = False
    file_store.put(login(access="relogin-access", refresh="relogin-refresh", expires_in=3600))
    file_store.read()
    cache = file_store.cache()
    assert not {"refresh_dead", "refresh_failed", "refresh_retry_at"} & set(cache)


def test_an_account_on_hold_has_its_own_message_and_is_retried_later(file_store):
    file_store.put(login())
    file_store.answer = lambda: http_error(403, {"error": "access_denied",
                                                 "error_description": "account_on_hold: see https://claude.ai/restricted"})
    file_store.read()
    assert file_store.outcomes == ["on-hold"]
    cache = file_store.cache()
    assert cache["refresh_failed"] == "on-hold" and "refresh_dead" not in cache
    assert cache["refresh_retry_at"] > time.time() + 590
    assert file_store.read()["error"] == file_store.core.t("err.login_on_hold")
    assert " | refresh-on-hold | pick=claude-b | account on hold | " in file_store.log("decisions.log")
    file_store.answer = answer()
    file_store.end_cooldown()
    file_store.read()
    assert len(file_store.token_calls) == 2


def test_an_expired_refresh_token_is_not_sent(file_store):
    file_store.put(login(refreshTokenExpiresAt=int((time.time() - 60) * 1000)))
    reading = file_store.read()
    assert file_store.starts == [] and file_store.token_calls == []
    assert reading["error"] == file_store.core.t("err.login_dead")


def test_qr_status_says_a_dead_login_needs_claude_login(file_store, monkeypatch, capsys):
    from quota_router import i18n
    core = file_store.core
    monkeypatch.setattr(core, "claude_reading", lambda acc, cache, force=False: core.cached_reading("claude", acc, cache, force))
    file_store.put(login())
    file_store.answer = dead_answer()
    core.status()
    capsys.readouterr()
    core.status()
    out = capsys.readouterr().out
    assert core.t("err.login_dead") in out and core.t("err.expired") not in out
    assert "claude login" in i18n.TABLES["zh-TW"]["err.login_dead"]
    assert "claude login" in i18n.TABLES["zh-TW"]["err.login_on_hold"]


def test_an_expired_cooldown_is_cleared_from_the_cache(file_store):
    """The outcome of an earlier attempt is dropped once its cooldown is over and a new attempt starts."""
    file_store.child = False
    file_store.put(login())
    cache = file_store.core.load("cache.json", {})
    cache["claude-b"] = {"token_sig": file_store.core.fingerprint(OLD_ACCESS),          # the same login as before
                         "refresh_failed": "failed", "refresh_retry_at": time.time() - 1}
    file_store.core.save("cache.json", cache)
    file_store.read()
    assert file_store.starts == ["claude-b"]
    assert not {"refresh_failed", "refresh_retry_at"} & set(file_store.cache())


class TokenServer(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.seen.append((self.path, self.headers.get("User-Agent"), body))
        if self.server.redirect_to:
            self.send_response(self.server.status)
            self.send_header("Location", self.server.redirect_to)
            self.end_headers()
            return
        reply = json.dumps(answer()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass


@pytest.fixture
def token_servers():
    """Two loopback token endpoints: `first` may redirect to `second`, which records what reaches it."""
    started = []
    for _ in range(2):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), TokenServer)
        server.seen, server.redirect_to, server.status = [], None, 307
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
    yield started
    for server in started:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("status", [None, 301, 302, 307, 308])
def test_the_token_request_on_the_wire_and_no_redirect_followed(router, monkeypatch, token_servers, status):
    core = router.core
    monkeypatch.setattr(core, "open_url", lambda req, timeout: core.OPENER.open(req, timeout=timeout))   # the real one, loopback only
    first, second = token_servers
    monkeypatch.setitem(core.ENDPOINTS, "claude_token", f"http://127.0.0.1:{first.server_address[1]}/v1/oauth/token")
    if status:
        first.status, first.redirect_to = status, f"http://127.0.0.1:{second.server_address[1]}/stolen"
    body = json.dumps({"grant_type": "refresh_token", "refresh_token": OLD_REFRESH}).encode()
    if status:
        with pytest.raises(urllib.error.HTTPError) as failed:
            core.post_token(body)
        assert failed.value.code == status
    else:
        assert core.post_token(body)["access_token"] == NEW_ACCESS
    assert first.seen == [("/v1/oauth/token", f"quota-router/{core.__version__}", body)]
    assert second.seen == []                     # the refresh token went to the configured address and nowhere else


# ---- after the answer: nothing may lose the new refresh token ------------------------------------------------------
class RotatingServer:
    """A token endpoint that rotates: once a refresh token is used it is dead. Its answer takes `delay` seconds; a
    socket that waits less than that gives up with a timeout, after the server has already rotated."""

    def __init__(self, delay=0.0):
        self.delay, self.live, self.calls = delay, {OLD_REFRESH}, 0

    def __call__(self, store):
        def reply():
            self.calls += 1
            sent = store.token_calls[-1]["body"]["refresh_token"]
            if sent not in self.live:
                return dead_answer()()
            self.live.discard(sent)
            self.live.add(f"rot-{self.calls}")
            if self.delay > store.token_calls[-1]["timeout"]:
                return TimeoutError("timed out")
            return answer(access_token=f"acc-{self.calls}", refresh_token=f"rot-{self.calls}")
        return reply


@pytest.mark.parametrize("delay", [4.5, 6.5, 25])
def test_a_slow_answer_after_rotation_is_still_saved(file_store, delay):
    """Round 1 gave up after 4 s inside the hook's 8 s; the background renewal waits 30 s, like Claude Code."""
    server = RotatingServer(delay)
    file_store.answer = server(file_store)
    file_store.put(login())
    with file_store.core.budget(time.monotonic() + 8):
        file_store.read()
    assert file_store.stored()["claudeAiOauth"]["refreshToken"] in server.live


def test_the_signals_that_stop_a_process_are_ignored_from_the_post_to_the_write(file_store, monkeypatch):
    if not hasattr(signal, "SIGTERM"):
        pytest.skip("no SIGTERM")
    seen = []
    import threading
    real = file_store.pc.save_claude_login

    def save(where, text, run):
        seen.append(("save", signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)))
        return real(where, text, run)
    monkeypatch.setattr(file_store.pc, "save_claude_login", save)
    file_store.during_token = lambda: seen.append(("post", signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)))
    before = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))
    assert threading.current_thread() is threading.main_thread()
    file_store.put(login())
    file_store.read()
    assert [s[0] for s in seen] == ["post", "save"]
    assert all(s[1] == signal.SIG_IGN and s[2] == signal.SIG_IGN for s in seen)
    assert (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)) == before


@pytest.mark.parametrize("failures, outcome", [(1, "renewed"), (2, "renewed"), (3, "unsaved")])
def test_the_write_is_tried_three_times(store, monkeypatch, failures, outcome):
    store.put(login())
    if store.mode == "file":
        real = store.pc.replace
        left = {"n": failures}

        def refuse(source, target):
            if target.endswith(".credentials.json") and left["n"]:
                left["n"] -= 1
                raise PermissionError(13, "denied")
            return real(source, target)
        monkeypatch.setattr(store.pc, "replace", refuse)
    else:
        store.fail_write = failures
    store.read()
    assert store.outcomes == [outcome] and len(store.token_calls) == 1
    saved = store.stored()["claudeAiOauth"]
    if outcome == "renewed":
        assert saved["refreshToken"] == NEW_REFRESH
        assert " | refresh | pick=claude-b | login renewed | " in store.log("decisions.log")
        return
    assert saved["accessToken"] == OLD_ACCESS                                             # nothing half-written
    assert store.read()["error"] == store.core.t("err.refresh_unsaved")
    assert " | login-refresh-save | " in store.log("hook-errors.log")
    assert " | refresh-unsaved | pick=claude-b | renewed but could not be saved | " in store.log("decisions.log")
    if store.mode == "file":
        assert [n for n in os.listdir(store.dir) if n.endswith(".tmp")] == []


def test_a_write_that_does_not_read_back_is_not_taken_for_a_renewal(file_store, monkeypatch):
    monkeypatch.setattr(file_store.pc, "save_claude_login", lambda where, text, run: None)     # says yes, writes nothing
    file_store.put(login())
    file_store.read()
    assert file_store.outcomes == ["unsaved"]
    assert " | refresh-unsaved | " in file_store.log("decisions.log")


def test_a_read_back_that_finds_nothing_is_a_softer_line_than_unsaved(file_store, monkeypatch):
    file_store.put(login())
    real = file_store.core.stored_claude_login
    calls = {"saved": False}
    real_save = file_store.pc.save_claude_login

    def save(where, text, run):
        real_save(where, text, run)
        calls["saved"] = True
    monkeypatch.setattr(file_store.pc, "save_claude_login", save)
    monkeypatch.setattr(file_store.core, "stored_claude_login",
                        lambda acc: (None, None, None) if calls["saved"] else real(acc))
    reading = file_store.read()
    assert file_store.outcomes == ["unverified"]
    assert file_store.stored()["claudeAiOauth"]["refreshToken"] == NEW_REFRESH
    log = file_store.log("decisions.log")
    assert " | refresh-unverified | pick=claude-b | renewed and saved; read-back failed | " in log
    assert " | refresh-unsaved | " not in log
    cache = file_store.cache()
    assert "refresh_failed" not in cache
    monkeypatch.setattr(file_store.core, "stored_claude_login", real)
    assert file_store.read().get("error") != file_store.core.t("err.refresh_unsaved")


@pytest.mark.skipif(WINDOWS, reason="keychain")
@pytest.mark.parametrize("hiccup", ["before-write", "read-back"])
def test_one_keychain_read_that_does_not_answer_after_the_post_loses_nothing(router, write_config, monkeypatch, hiccup):
    store = make_store(router, write_config, monkeypatch, "keychain")
    server = RotatingServer()
    store.answer = server(store)
    store.put(login())
    real_run = store.run
    state = {"saved": False, "failed": False}

    def run(argv, input=None):
        if argv == ["security", "-i"]:
            state["saved"] = True
        posted = bool(store.token_calls)
        if (posted and not state["failed"] and argv[:2] == ["security", "find-generic-password"]
                and state["saved"] == (hiccup == "read-back")):
            state["failed"] = True
            return None
        return real_run(argv, input)
    monkeypatch.setattr(store.core, "run_local", run)
    store.read()
    assert state["failed"] and store.stored()["claudeAiOauth"]["refreshToken"] in server.live


def test_a_login_changed_during_the_renewal_is_not_overwritten(file_store):
    file_store.put(login())
    other = login(access="other-access", refresh="other-refresh")         # someone ignoring the locks wrote meanwhile
    file_store.during_token = lambda: file_store.put(other)
    file_store.read()
    assert file_store.stored() == other
    assert " | refresh-unsaved | pick=claude-b | " in file_store.log("decisions.log")
    assert " | login-refresh-save | " in file_store.log("hook-errors.log")


# ---- the write itself ---------------------------------------------------------------------------------------------
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


def test_read_processes_tells_an_unreadable_list_from_an_empty_one(monkeypatch):
    from quota_router import platform_compat as pc
    monkeypatch.setattr(pc, "system", lambda: "linux")
    assert pc.read_processes(lambda argv: None) is None
    assert pc.read_processes(lambda argv: result(1, "")) is None
    assert pc.read_processes(lambda argv: result(0, "")) is None              # ps always lists itself
    assert pc.read_processes(lambda argv: result(0, "  7 1 claude\n")) == {7: (1, "claude")}
    assert pc.list_processes(lambda argv: None) == {}
    monkeypatch.setattr(pc, "system", lambda: "windows")
    monkeypatch.setattr(pc, "_windows_processes", None)
    assert pc.read_processes(lambda argv: result(0, '[{"ProcessId": 3, "Par')) is None
    assert pc.read_processes(lambda argv: result(0, "")) == {}


# ---- docs ---------------------------------------------------------------------------------------------------------
def test_help_and_docs_no_longer_promise_read_only(router):
    from quota_router import i18n
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for language in ("en", "zh-TW"):
        text = i18n.TABLES[language]["help"]
        assert "Read-only" not in text and "never refreshed" not in text and "只讀" not in text and "不刷新" not in text
        assert "QR_NO_REFRESH" in text
    for name in ("README.md", "README.zh-TW.md", "docs/REFERENCE.md"):
        with open(os.path.join(root, name), encoding="utf-8") as f:
            text = f.read()
        for phrase in ("never refreshes", "read-only.", "never refreshed or written back", "不刷新、不寫回"):
            assert phrase not in text, (name, phrase)


def test_the_user_agent_follows_the_release_version(router):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "pyproject.toml"), encoding="utf-8") as f:
        version = re.search(r'^version = "([^"]+)"', f.read(), re.M)[1]
    assert router.core.USER_AGENT == f"quota-router/{version}"
