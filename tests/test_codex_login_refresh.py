"""quota-router renews a Codex login before it expires and writes it back into auth.json; a failed renewal of any
family runs the [notify] command, once per account and reason within repeat_hours.

A reading (the hook, qr status / pick / check) never contacts the token endpoint: it starts one background process
(`renew-login <key>`). Here that process runs in-process (CodexStore.spawn), right where the reading started it.
Nothing reaches the network, the real ~/.codex or the real process table.
"""
import base64
import io
import json
import os
import stat
import sys
import time
import urllib.error

import shlex
import subprocess

import pytest

from conftest import CONFIG, WINDOWS, posix_only
from quota_router import core as _core

REAL_RUN_NOTIFY_COMMAND = _core.run_notify_command
REAL_SUBPROCESS_RUN = subprocess.run

TOKEN_URL = "http://127.0.0.1:9/codex-token"
USAGE_URL = "http://127.0.0.1:9/codex-usage"
ENDPOINTS = f'[endpoints]\ncodex_usage = "{USAGE_URL}"\ncodex_token = "{TOKEN_URL}"\n'
OLD_REFRESH, NEW_REFRESH = "old-refresh-FAKE", "new-refresh-FAKE"
HOUR = 3600


def jwt(**claims):
    def part(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    return f"{part({'alg': 'none'})}.{part(claims)}.c2ln"


def access(hours, tag="old"):
    """An access token that expires `hours` from now (negative: already expired)."""
    return jwt(exp=int(time.time() + hours * HOUR), tag=tag)


ID_TOKEN = jwt(email="b@example.test", exp=int(time.time() + HOUR))


def auth(hours=24, refresh=OLD_REFRESH, **extra):
    data = {"OPENAI_API_KEY": None, "auth_mode": "chatgpt",
            "tokens": {"id_token": ID_TOKEN, "access_token": access(hours), "refresh_token": refresh,
                       "account_id": "acct-1"},
            "last_refresh": "2026-10-10T00:00:00.000000Z"}
    data.update(extra)
    return data


def answer(**override):
    data = {"id_token": jwt(email="b@example.test", exp=int(time.time() + HOUR), fresh=True),
            "access_token": access(240, "new"), "refresh_token": NEW_REFRESH}
    data.update(override)
    return {k: v for k, v in data.items() if v is not None}


def http_error(code, body):
    return urllib.error.HTTPError(TOKEN_URL, code, "Error", {}, io.BytesIO(json.dumps(body).encode()))


USAGE = {"rate_limit": {"allowed": True, "limit_reached": False,
                        "primary_window": {"used_percent": 10, "reset_at": time.time() + 3600, "limit_window_seconds": 18000},
                        "secondary_window": {"used_percent": 20, "reset_at": time.time() + 86400, "limit_window_seconds": 604800}},
         "credits": {"balance": 0}}


class CodexStore:
    """The fake machine: codex-2's auth.json, the token endpoint, the process list and the [notify] command."""

    def __init__(self, router, monkeypatch):
        from quota_router import platform_compat
        self.core, self.home = router.core, router.home
        self.acc = next(a for a in self.core.CODEX_ACCOUNTS if a["key"] == "codex-2")
        self.dir = self.acc["dir"]
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, "auth.json")
        self.token_calls, self.starts, self.outcomes, self.alerts, self.alert_envs = [], [], [], [], []
        self.alert_context = []
        self.answer = answer()
        self.during_token = None
        self.procs, self.envs = {}, {}
        self.notify_code = 0
        monkeypatch.setattr(self.core, "open_url", self.open_url)
        monkeypatch.setattr(self.core, "spawn_renewal", self.spawn)
        monkeypatch.setattr(self.core, "REFRESH_WRITE_PAUSES", (0, 0, 0))
        monkeypatch.setattr(self.core, "REFRESH_READBACK_PAUSES", (0, 0, 0))
        monkeypatch.setattr(self.core, "RENEWALS", [])
        monkeypatch.setattr(self.core, "run_notify_command", self.notify)
        monkeypatch.setattr(platform_compat, "read_processes", lambda run: None if self.procs is None else dict(self.procs))
        monkeypatch.setattr(platform_compat, "process_environment", lambda pid, command, run: self.envs.get(pid))

    def put(self, data):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        if not WINDOWS:
            os.chmod(self.path, 0o600)

    def stored(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def spawn(self, acc):
        self.starts.append(acc["key"])
        with self.core.budget(None):
            self.outcomes.append(self.core.renew_codex_login(acc))
        return None

    def notify(self, command, env, log):
        import signal
        self.alerts.append({k: v for k, v in env.items() if k.startswith("QR_")})
        self.alert_envs.append(dict(env))
        # where it runs: the account's lock is free again and stop signals are not ignored any more
        with self.core.state_lock("refresh-" + env["QR_KEY"], timeout=0) as free:
            self.alert_context.append((free, signal.getsignal(signal.SIGTERM) is not signal.SIG_IGN))
        if isinstance(self.notify_code, BaseException):
            raise self.notify_code
        return self.notify_code

    def open_url(self, request, timeout):
        url = request.full_url
        if url == TOKEN_URL:
            self.token_calls.append({"method": request.get_method(), "type": request.get_header("Content-type"),
                                     "agent": request.get_header("User-agent"), "body": json.loads(request.data)})
            if self.during_token:
                self.during_token()
            reply = self.answer() if callable(self.answer) else self.answer
            if isinstance(reply, BaseException):
                raise reply
            return io.BytesIO(json.dumps(reply).encode())
        if url == USAGE_URL:
            return io.BytesIO(json.dumps(USAGE).encode())
        raise AssertionError("unexpected URL " + url)

    def read(self):
        return self.core.cached_reading("codex", self.acc, {})

    def again(self):
        """Forget the last reading, the cooldown and the start marker."""
        (self.home / "state" / "cache.json").unlink(missing_ok=True)
        (self.home / "state" / "refresh-codex-2.start").unlink(missing_ok=True)

    def decisions(self):
        path = self.home / "state" / "decisions.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def busy_in_reading(self):
        """The reading itself saw the process: it started nothing and set the short cooldown."""
        return (self.starts == [] and self.token_calls == []
                and self.core.load("cache.json", {})[self.acc["key"]].get("refresh_failed") == "busy")

    def end_cooldown(self):
        cache = self.core.load("cache.json", {})
        cache[self.acc["key"]]["refresh_retry_at"] = 0
        self.core.save("cache.json", cache)
        (self.home / "state" / f"refresh-{self.acc['key']}.start").unlink(missing_ok=True)

    def run_on(self, cmd, directory, pid=101):
        """One process `cmd` running with CODEX_HOME=`directory` (None: the variable unset)."""
        self.procs[pid] = (1, cmd)
        self.envs[pid] = "HOME=/x TERM=xterm" + (f" CODEX_HOME={directory}" if directory is not None else "")


def make_store(router, write_config, monkeypatch, extra="[families.codex]\nrefresh_logins = true\n"):
    from quota_router import config
    write_config(extra + ENDPOINTS + CONFIG)
    router.core.configure(config.load())
    return CodexStore(router, monkeypatch)


@pytest.fixture
def codex(router, write_config, monkeypatch):
    return make_store(router, write_config, monkeypatch,
                      '[families.codex]\nrefresh_logins = true\n[notify]\non_refresh_failed = "alert"\n')


# ---- when a Codex login is due ----------------------------------------------------------------------------------
def test_a_login_expiring_within_the_margin_is_renewed_and_written_back(codex):
    codex.put(auth(hours=24))
    reading = codex.read()
    assert codex.starts == ["codex-2"] and codex.outcomes == ["renewed"]
    call, = codex.token_calls
    assert call["method"] == "POST" and call["type"] == "application/json"
    assert call["body"] == {"client_id": "app_EMoamEEZ73f0CkXaXp7hrann", "grant_type": "refresh_token",
                            "refresh_token": OLD_REFRESH}
    assert call["agent"] == codex.core.USER_AGENT
    stored = codex.stored()
    assert stored["tokens"]["access_token"] == codex.answer["access_token"]
    assert stored["tokens"]["refresh_token"] == NEW_REFRESH
    assert stored["tokens"]["id_token"] == codex.answer["id_token"]
    assert stored["tokens"]["account_id"] == "acct-1"                       # everything else is kept
    assert stored["auth_mode"] == "chatgpt" and "OPENAI_API_KEY" in stored and stored["OPENAI_API_KEY"] is None
    assert stored["last_refresh"].endswith("Z") and stored["last_refresh"] > "2026-10-10"
    if not WINDOWS:
        assert stat.S_IMODE(os.stat(codex.path).st_mode) == 0o600
    assert not [n for n in os.listdir(codex.dir) if n.endswith(".tmp")]
    assert codex.core.t("note.refresh_started") in reading.get("notes_extra", "")
    log = codex.decisions()
    assert "| codex | refresh-started | pick=codex-2 |" in log and "| codex | refresh | pick=codex-2 | login renewed |" in log
    assert codex.alerts == []                                                # nothing failed


def test_a_login_with_more_time_left_is_left_alone(codex):
    codex.put(auth(hours=73))
    codex.read()
    assert codex.starts == [] and codex.token_calls == []


def test_the_margin_is_a_setting(router, write_config, monkeypatch):
    codex = make_store(router, write_config, monkeypatch,
                       "[families.codex]\nrefresh_logins = true\nrefresh_before_hours = 100\n")
    codex.put(auth(hours=90))
    codex.read()
    assert codex.outcomes == ["renewed"]


def test_codex_renewal_is_off_by_default(router, write_config, monkeypatch):
    codex = make_store(router, write_config, monkeypatch, "")
    codex.put(auth(hours=-1))
    codex.read()
    assert codex.starts == [] and codex.token_calls == []
    assert codex.core.renew_codex_login(codex.acc) == "off"


def test_qr_no_refresh_turns_it_off(codex, monkeypatch):
    monkeypatch.setenv("QR_NO_REFRESH", "1")
    codex.put(auth(hours=1))
    codex.read()
    assert codex.starts == [] and codex.token_calls == []


def test_without_a_readable_exp_the_age_of_last_refresh_decides(codex):
    """Codex itself renews eight days after last_refresh when the access token's expiry cannot be read."""
    data = auth()
    data["tokens"]["access_token"] = "not-a-jwt"
    data["last_refresh"] = "2026-01-01T00:00:00.123456789Z"                # Codex writes nanoseconds
    codex.put(data)
    codex.read()
    assert codex.outcomes == ["renewed"]
    codex.again()
    codex.put(dict(data, last_refresh=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
    codex.read()
    assert len(codex.token_calls) == 1


@pytest.mark.parametrize("change", [
    lambda d: d["tokens"].pop("refresh_token"),
    lambda d: d.update(auth_mode="apikey"),
])
def test_a_login_that_is_not_codexs_own_is_left_alone(codex, change):
    data = auth(hours=1)
    change(data)
    codex.put(data)
    codex.read()
    assert codex.token_calls == []


def test_someone_else_renewed_it_before_the_background_process_ran(codex):
    codex.put(auth(hours=1))
    real = codex.spawn

    def spawn(acc):
        codex.put(auth(hours=240, refresh="codex-renewed-it"))
        return real(acc)
    codex.core.spawn_renewal = spawn
    codex.read()
    assert codex.outcomes == ["adopted"] and codex.token_calls == []


# ---- Codex processes ----------------------------------------------------------------------------------------------
JOBS = [
    "codex exec --skip-git-repo-check hi",
    "/opt/homebrew/bin/codex",                                               # an interactive session
    "node /opt/homebrew/lib/node_modules/@openai/codex/bin/codex.js exec hi",
]
DESKTOP_APP_SERVER = ("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex "
                      "-c features.code_mode_host=true app-server --analytics-default-enabled")
RESIDENTS = [DESKTOP_APP_SERVER, "codex app-server daemon pid-update-loop", "codex mcp-server"]


@pytest.mark.parametrize("cmd", JOBS + RESIDENTS)
def test_a_codex_process_on_that_home_does_not_block_a_renewal_well_before_the_expiry(codex, cmd):
    """Codex renews on its own only within five minutes of the expiry (or after a 401, re-reading auth.json first)."""
    codex.put(auth(hours=24))
    codex.run_on(cmd, codex.dir)
    codex.read()
    assert codex.outcomes == ["renewed"]


@pytest.mark.parametrize("cmd", JOBS + RESIDENTS)
@pytest.mark.parametrize("hours", [0.4, -0.1, -1, -240])
def test_any_codex_process_on_that_home_blocks_it_close_to_or_after_the_expiry(codex, cmd, hours):
    """Once the token has expired every use renews it (expires_at <= now + 5 min), however long ago it expired."""
    codex.put(auth(hours=hours))
    codex.run_on(cmd, codex.dir)
    codex.read()
    assert codex.busy_in_reading()
    # the background process, had it been started, says the same
    assert codex.core.renew_codex_login(codex.acc) == "busy" and codex.token_calls == []
    assert "| codex | refresh-busy | pick=codex-2 | Codex may be using that directory |" in codex.decisions()
    assert codex.alerts == []                                                # busy is not a failure


@pytest.mark.parametrize("cmd", ["codex login", "codex logout", "codex -c x=y login --device-auth"])
def test_codex_login_on_that_home_always_blocks_it(codex, cmd):
    codex.put(auth(hours=60))
    codex.run_on(cmd, codex.dir)
    codex.read()
    assert codex.busy_in_reading()


def test_a_codex_process_on_another_home_does_not_count(codex):
    codex.put(auth(hours=0.2))
    codex.run_on("codex exec hi", None)                                      # ~/.codex, codex-1
    codex.run_on("codex login", str(codex.home / "elsewhere"), pid=102)
    codex.run_on(DESKTOP_APP_SERVER, None, pid=103)
    codex.read()
    assert codex.outcomes == ["renewed"]


def test_the_default_home_is_codex_1s(router, write_config, monkeypatch):
    codex = make_store(router, write_config, monkeypatch)
    codex.acc = next(a for a in codex.core.CODEX_ACCOUNTS if a["key"] == "codex-1")
    codex.dir = codex.acc["dir"]
    os.makedirs(codex.dir, exist_ok=True)
    codex.path = os.path.join(codex.dir, "auth.json")
    codex.put(auth(hours=-2))
    codex.run_on(DESKTOP_APP_SERVER, None)                                   # the desktop app, CODEX_HOME unset
    codex.read()
    assert codex.busy_in_reading()


def test_an_unreadable_process_list_counts_as_busy(codex):
    codex.put(auth(hours=24))
    codex.procs = None
    codex.read()
    assert codex.outcomes == [] and codex.starts == [] and codex.token_calls == []   # the reading already saw it
    assert codex.core.load("cache.json", {})["codex-2"]["refresh_failed"] == "busy"


@pytest.mark.skipif(WINDOWS, reason="Windows never reads another process's environment")
@pytest.mark.parametrize("cmd,hours", [("codex exec hi", 0.2), ("codex login", 60)])
def test_a_counting_codex_process_whose_environment_cannot_be_read_counts_as_busy(codex, cmd, hours):
    codex.put(auth(hours=hours))
    codex.procs = {101: (1, cmd)}
    codex.read()
    assert codex.starts == [] and codex.token_calls == []


def test_the_background_process_checks_the_processes_again(codex):
    codex.put(auth(hours=24))
    real = codex.spawn

    def spawn(acc):
        codex.run_on("codex login", codex.dir)                               # started right after the reading
        return real(acc)
    codex.core.spawn_renewal = spawn
    codex.read()
    assert codex.outcomes == ["busy"] and codex.token_calls == []


def test_the_classifier(router):
    classify = router.core.classify_codex_process
    assert classify("codex exec hi") == "job"
    assert classify("codex") == "job"
    assert classify("codex resume --last") == "job"
    assert classify("codex login") == "login" and classify("codex logout") == "login"
    assert classify(DESKTOP_APP_SERVER) == "resident"
    assert classify("/x/codex app-server daemon pid-update-loop") == "resident"
    assert classify("codex mcp-server") == "resident"
    assert classify("codex exec 'start the app-server'") == "job"
    assert classify("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex-code-mode-host") is None
    assert classify("/Applications/ChatGPT.app/Contents/Frameworks/Codex Framework.framework/Helpers/Codex (Renderer)") is None
    assert classify("claude -p hi") is None


# ---- failures -----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("reply", [
    lambda: http_error(401, {"error": {"code": "refresh_token_expired", "message": "expired"}}),
    lambda: http_error(400, {"error": "invalid_grant", "error_description": "bad"}),
    lambda: http_error(400, {"error": {"code": "refresh_token_reused"}}),
    lambda: http_error(400, {"code": "refresh_token_invalidated"}),
    lambda: http_error(401, {}),
    lambda: http_error(403, {"error": {"code": "refresh_token_reused"}}),     # the code decides, not the status
    lambda: http_error(500, {"code": "refresh_token_expired"}),
])
def test_a_refused_refresh_token_asks_for_codex_login_and_alerts(codex, reply):
    codex.put(auth(hours=24))
    codex.answer = reply
    reading = codex.read()
    assert codex.outcomes == ["dead"]
    assert codex.stored()["tokens"]["refresh_token"] == OLD_REFRESH            # untouched
    assert "| codex | refresh-dead | pick=codex-2 | refresh token refused; run codex login there |" in codex.decisions()
    alert, = codex.alerts
    assert alert["QR_FAMILY"] == "codex" and alert["QR_KEY"] == "codex-2" and alert["QR_REASON"] == "dead"
    assert alert["QR_DIR"] == codex.dir and alert["QR_EMAIL"] == "b@example.test"
    assert alert["QR_EXPIRES_AT"][:4] >= "2026" and alert["QR_DETAIL"] == "refresh token refused; run codex login there"
    # the same dead token is never sent again, and later readings say what to do
    codex.end_cooldown()
    reading = codex.read()
    assert len(codex.token_calls) == 1 and codex.starts == ["codex-2"] and len(codex.alerts) == 1
    assert codex.core.t("err.codex_login_dead") in reading.get("notes_extra", "")
    assert "codex login" in codex.core.i18n.TABLES["zh-TW"]["err.codex_login_dead"]


def test_a_refusal_after_codex_renewed_it_meanwhile_is_not_a_dead_login(codex):
    """Codex renewed in the same second: the refusal is for the old token, the file already holds a working login."""
    codex.put(auth(hours=24))
    codex.during_token = lambda: codex.put(auth(hours=240, refresh="codex-renewed-it"))
    codex.answer = lambda: http_error(401, {"error": {"code": "refresh_token_reused"}})
    codex.read()
    assert codex.outcomes == ["adopted"] and codex.alerts == []
    assert "refresh_dead" not in codex.core.load("cache.json", {})["codex-2"]
    assert codex.stored()["tokens"]["refresh_token"] == "codex-renewed-it"


@pytest.mark.parametrize("same_access", [True, False])
def test_logging_in_again_ends_the_dead_mark(codex, same_access):
    """`codex login` again on the same account keeps account_id; a new refresh token (or a new access token) ends it."""
    data = auth(hours=24)
    codex.put(data)
    codex.answer = lambda: http_error(401, {})
    codex.read()
    assert codex.core.load("cache.json", {})["codex-2"]["refresh_failed"] == "dead"
    again = auth(hours=240, refresh="after-codex-login")
    if same_access:
        again["tokens"]["access_token"] = data["tokens"]["access_token"]
    codex.put(again)
    (codex.home / "state" / "refresh-codex-2.start").unlink(missing_ok=True)
    reading = codex.read()
    cached = codex.core.load("cache.json", {})["codex-2"]
    assert not {"refresh_failed", "refresh_retry_at", "refresh_dead"} & set(cached)
    assert codex.core.t("err.codex_login_dead") not in (reading.get("notes_extra") or "")


@pytest.mark.parametrize("code", [429, 500, 503, 403])
def test_other_error_answers_are_a_failure_with_a_cooldown(codex, code):
    codex.put(auth(hours=24))
    codex.answer = lambda: http_error(code, {"error": "nope"})
    codex.read()
    assert codex.outcomes == ["failed"]
    assert f"| codex | refresh-failed | pick=codex-2 | renewal failed, HTTP {code} |" in codex.decisions()
    assert codex.alerts == []                                                # one temporary failure is not told
    assert "| codex | notify-skipped | pick=codex-2 | alert waits for another failure |" in codex.decisions()
    codex.read()                                                             # cooldown: not tried again
    assert len(codex.token_calls) == 1


def test_an_answer_without_an_access_token_still_keeps_its_new_refresh_token(codex):
    data = auth(hours=24)
    codex.put(data)
    codex.answer = answer(access_token=None, id_token=None)
    codex.read()
    assert codex.outcomes == ["failed"]
    stored = codex.stored()["tokens"]
    assert stored["refresh_token"] == NEW_REFRESH
    assert stored["access_token"] == data["tokens"]["access_token"] and stored["id_token"] == ID_TOKEN
    assert "| codex | refresh-failed | pick=codex-2 | odd answer; new refresh token kept |" in codex.decisions()


def test_an_unreadable_id_token_is_not_stored(codex):
    """Codex refuses to load an auth.json whose id_token is not a JWT; the old one stays."""
    codex.put(auth(hours=24))
    codex.answer = answer(id_token="garbage")
    codex.read()
    assert codex.outcomes == ["renewed"]
    assert codex.stored()["tokens"]["id_token"] == ID_TOKEN


@pytest.mark.parametrize("failures,outcome", [(1, "renewed"), (2, "renewed"), (3, "unsaved")])
def test_the_write_is_tried_three_times(codex, monkeypatch, failures, outcome):
    codex.put(auth(hours=24))
    real = codex.core.save_codex_auth
    left = [failures]

    def save(path, data):
        if left[0]:
            left[0] -= 1
            raise OSError("disk full")
        return real(path, data)
    monkeypatch.setattr(codex.core, "save_codex_auth", save)
    codex.read()
    assert codex.outcomes == [outcome]
    if outcome == "unsaved":
        assert codex.stored()["tokens"]["refresh_token"] == OLD_REFRESH
        assert "| codex | refresh-unsaved | pick=codex-2 |" in codex.decisions()
        assert [a["QR_REASON"] for a in codex.alerts] == ["unsaved"]
        assert "login-refresh-save" in (codex.home / "state" / "hook-errors.log").read_text()
    else:
        assert codex.stored()["tokens"]["refresh_token"] == NEW_REFRESH and codex.alerts == []


def test_a_login_changed_during_the_post_is_not_overwritten(codex):
    codex.put(auth(hours=24))
    codex.during_token = lambda: codex.put(auth(hours=240, refresh="someone-logged-in-again"))
    codex.read()
    assert codex.outcomes == ["conflict"]
    assert codex.stored()["tokens"]["refresh_token"] == "someone-logged-in-again"
    assert [a["QR_REASON"] for a in codex.alerts] == ["unsaved"]


def test_a_symlinked_auth_json_is_written_through(codex):
    if WINDOWS:
        pytest.skip("symbolic links need privileges on Windows")
    real = os.path.join(codex.dir, "real-auth.json")
    codex.path = real
    codex.put(auth(hours=24))
    os.symlink(real, os.path.join(codex.dir, "auth.json"))
    codex.path = os.path.join(codex.dir, "auth.json")
    codex.read()
    assert codex.outcomes == ["renewed"]
    assert os.path.islink(codex.path)
    with open(real, encoding="utf-8") as f:
        assert json.load(f)["tokens"]["refresh_token"] == NEW_REFRESH


def test_a_missing_auth_json_is_never_created(router, home):
    path = home / "nowhere" / "auth.json"
    with pytest.raises(FileNotFoundError):
        router.core.save_codex_auth(str(path), {"tokens": {}})
    assert not path.exists()


def test_renew_login_finds_codex_accounts(codex, capsys):
    codex.put(auth(hours=24))
    assert codex.core.renewal_main(["codex-2"]) == 0
    assert capsys.readouterr().out.strip().endswith("renewed")
    assert codex.core.renewal_main(["no-such-account"]) == 2


def test_no_token_and_no_answer_reaches_a_log(codex):
    codex.put(auth(hours=24))
    codex.answer = lambda: http_error(401, {"error": {"code": "refresh_token_expired", "message": OLD_REFRESH}})
    codex.read()
    for name in os.listdir(codex.home / "state"):
        text = (codex.home / "state" / name).read_text(encoding="utf-8", errors="replace")
        assert OLD_REFRESH not in text and NEW_REFRESH not in text and "b@example.test" not in text, name


# ---- the [notify] command -----------------------------------------------------------------------------------------
def test_an_alert_is_sent_once_per_account_and_reason_within_repeat_hours(codex, monkeypatch):
    codex.put(auth(hours=24))
    codex.answer = lambda: http_error(401, {})
    codex.read()
    codex.again()
    codex.read()
    assert len(codex.token_calls) == 2 and [a["QR_REASON"] for a in codex.alerts] == ["dead"]
    assert "| codex | notify-skipped | pick=codex-2 | alert already sent |" in codex.decisions()
    # another reason is told at once
    codex.core.notify_refresh_failed("codex", codex.acc, "unsaved", "renewed but could not be saved")
    assert [a["QR_REASON"] for a in codex.alerts] == ["dead", "unsaved"]
    # after repeat_hours the same reason is told again
    real_now = codex.core.now
    monkeypatch.setattr(codex.core, "now", lambda: real_now() + 12 * HOUR + 60)
    codex.core.notify_refresh_failed("codex", codex.acc, "dead", "refresh token refused; run codex login there")
    assert [a["QR_REASON"] for a in codex.alerts] == ["dead", "unsaved", "dead"]


def test_a_temporary_failure_is_told_only_when_it_repeats(codex):
    codex.put(auth(hours=24))
    codex.answer = lambda: http_error(503, {})
    codex.read()
    assert codex.alerts == []
    codex.again()
    codex.read()
    assert [a["QR_REASON"] for a in codex.alerts] == ["failed"]
    codex.again()
    codex.read()                                                             # the third within repeat_hours: not again
    assert len(codex.alerts) == 1 and len(codex.token_calls) == 3


def test_failed_renewed_failed_is_never_told(codex):
    """Claude renews every eight hours; one 429 between two successes must not be a message each time."""
    for _ in range(3):
        codex.put(auth(hours=24))
        codex.answer = lambda: http_error(429, {})
        codex.read()
        codex.again()
        codex.answer = answer()
        codex.read()
        codex.again()
    assert codex.outcomes == ["failed", "renewed"] * 3 and codex.alerts == []


def test_the_number_of_failures_is_a_setting(router, write_config, monkeypatch):
    codex = make_store(router, write_config, monkeypatch, '[families.codex]\nrefresh_logins = true\n'
                       '[notify]\non_refresh_failed = "alert"\nfailed_attempts = 1\n')
    codex.put(auth(hours=24))
    codex.answer = lambda: http_error(503, {})
    codex.read()
    assert [a["QR_REASON"] for a in codex.alerts] == ["failed"]


def test_a_successful_renewal_clears_the_alert_memory(codex):
    codex.put(auth(hours=24))
    codex.answer = lambda: http_error(401, {})
    codex.read()
    codex.put(auth(hours=24, refresh="after-codex-login"))
    codex.again()
    codex.answer = answer()
    codex.read()
    assert codex.outcomes == ["dead", "renewed"]
    assert codex.core.load("notify-state.json", {}) == {}
    codex.core.notify_refresh_failed("codex", codex.acc, "dead", "refresh token refused; run codex login there")
    assert len(codex.alerts) == 2


def test_without_the_state_lock_no_alert_runs(codex, monkeypatch):
    from contextlib import contextmanager
    real = codex.core.state_lock

    @contextmanager
    def lock(name, timeout=2):
        if name == "notify":
            yield False
        else:
            with real(name, timeout) as locked:
                yield locked
    monkeypatch.setattr(codex.core, "state_lock", lock)
    assert codex.core.notify_refresh_failed("codex", codex.acc, "dead", "x") == "skipped"
    assert codex.alerts == [] and "alert skipped, state lock busy" in codex.decisions()


def test_a_claim_that_never_reported_back_counts_as_a_failed_run(codex):
    """A run killed before it could record its outcome leaves {"ok": None}; it must not count as sent for 12 hours."""
    slot = "codex:codex-2:dead"
    codex.core.save("notify-state.json", {slot: {"at": time.time() - 2 * HOUR, "ok": None}})
    assert codex.core.notify_refresh_failed("codex", codex.acc, "dead", "x") == "sent"
    codex.core.save("notify-state.json", {slot: {"at": time.time() - 10 * 60, "ok": None}})
    assert codex.core.notify_refresh_failed("codex", codex.acc, "dead", "x") == "skipped"   # failed: an hour
    codex.core.save("notify-state.json", {slot: {"at": time.time() - 5, "ok": None}})
    assert codex.core.notify_refresh_failed("codex", codex.acc, "dead", "x") == "skipped"   # still running


def test_the_command_runs_after_the_lock_and_outside_the_signal_shield(codex):
    codex.put(auth(hours=24))
    codex.answer = lambda: http_error(401, {})
    codex.read()
    assert codex.alert_context == [(True, True)]


def test_the_command_gets_only_a_few_variables_of_the_environment(codex, monkeypatch):
    monkeypatch.setenv("SOME_SECRET_TOKEN", "do-not-pass")
    monkeypatch.setenv("LC_ALL", "zh_TW.UTF-8")
    monkeypatch.setenv("QR_CONFIG", "/elsewhere")
    codex.put(auth(hours=24))
    codex.answer = lambda: http_error(401, {})
    codex.read()
    env, = codex.alert_envs
    assert "SOME_SECRET_TOKEN" not in env and "QR_CONFIG" not in env
    assert env["LC_ALL"] == "zh_TW.UTF-8" and env["PATH"] == os.environ["PATH"] and env["HOME"] == os.environ["HOME"]
    assert {k for k in env if k.startswith("QR_")} == {"QR_FAMILY", "QR_KEY", "QR_REASON", "QR_DETAIL", "QR_DIR",
                                                       "QR_EMAIL", "QR_EXPIRES_AT"}


@pytest.mark.parametrize("failure", [1, 127, OSError("no shell"), "timeout"])
def test_a_failing_alert_command_changes_nothing_about_the_renewal(codex, failure):
    import subprocess
    codex.notify_code = subprocess.TimeoutExpired("alert", 30) if failure == "timeout" else failure
    codex.put(auth(hours=24))
    codex.answer = lambda: http_error(401, {})
    codex.read()
    assert codex.outcomes == ["dead"] and len(codex.alerts) == 1
    assert "| codex | refresh-dead | pick=codex-2 |" in codex.decisions()
    assert "| codex | notify-failed | pick=codex-2 | alert command failed |" in codex.decisions()
    assert codex.core.load("cache.json", {})["codex-2"]["refresh_dead"] == codex.core.fingerprint(OLD_REFRESH)
    # a failed command may run again after an hour, not only after repeat_hours
    state = codex.core.load("notify-state.json", {})
    assert state["codex:codex-2:dead"]["ok"] is False


def test_an_alert_command_that_raises_anything_is_contained(codex, monkeypatch):
    monkeypatch.setattr(codex.core, "run_notify", lambda *a: 1 / 0)
    codex.put(auth(hours=24))
    codex.answer = lambda: http_error(401, {})
    codex.read()
    assert codex.outcomes == ["dead"]
    assert "| notify | ZeroDivisionError |" in (codex.home / "state" / "hook-errors.log").read_text()


def test_no_command_configured_runs_nothing(router, write_config, monkeypatch):
    codex = make_store(router, write_config, monkeypatch)
    codex.put(auth(hours=24))
    codex.answer = lambda: http_error(401, {})
    codex.read()
    assert codex.outcomes == ["dead"] and codex.alerts == []


def test_busy_none_and_adopted_never_alert(codex):
    for outcome in ("busy", "none", "adopted", "renewed", "running", "off", "unverified"):
        codex.core.finish_renewal(codex.acc, outcome, family="codex")
    assert codex.alerts == []


def test_a_claude_failure_alerts_too(codex):
    acc = next(a for a in codex.core.CLAUDE_ACCOUNTS if a["key"] == "claude-b")
    codex.core.save("cache.json", {"claude-b": {"email": "c@example.test"}})
    codex.core.finish_renewal(acc, "dead", dead="abc")
    alert, = codex.alerts
    assert alert["QR_FAMILY"] == "claude" and alert["QR_KEY"] == "claude-b" and alert["QR_REASON"] == "dead"
    assert alert["QR_EMAIL"] == "c@example.test" and alert["QR_DIR"] == acc["dir"]
    assert alert["QR_DETAIL"] == "refresh token refused; run claude login there"


@posix_only
def test_the_command_runs_through_the_shell_with_the_variables(router, write_config, monkeypatch, home):
    out, script = home / "alert.txt", home / "alert.py"
    script.write_text("import os, sys\n"
                      "with open(sys.argv[1], 'a') as f:\n"
                      "    f.write(os.environ['QR_KEY'] + ' ' + os.environ['QR_REASON'] + ' ' + os.environ['QR_FAMILY'] + '\\n')\n"
                      "print('sent')\n")
    command = " ".join(shlex.quote(str(x)) for x in (sys.executable, script, out))
    assert "'" not in command
    codex = make_store(router, write_config, monkeypatch,
                       f"[families.codex]\nrefresh_logins = true\n[notify]\non_refresh_failed = '{command}'\n")
    monkeypatch.setattr(codex.core, "run_notify_command", REAL_RUN_NOTIFY_COMMAND)
    monkeypatch.setattr(subprocess, "run", REAL_SUBPROCESS_RUN)
    codex.put(auth(hours=24))
    codex.answer = lambda: http_error(401, {})
    codex.read()
    codex.again()
    codex.read()
    assert len(codex.token_calls) == 2
    assert out.read_text() == "codex-2 dead codex\n"                        # once, not twice
    log = (home / "state" / "notify.log").read_text()
    assert "codex codex-2 dead" in log and "sent" in log and "exit 0" in log


@posix_only
def test_a_command_that_runs_too_long_is_ended_with_everything_it_started(router, monkeypatch, home):
    late = home / "late.txt"
    monkeypatch.setattr(router.core, "NOTIFY_TIMEOUT_SEC", 0.5)
    monkeypatch.setattr(subprocess, "run", REAL_SUBPROCESS_RUN)
    command = f"(sleep 1.5; echo late > {shlex.quote(str(late))}) & wait"
    with open(home / "out.log", "w") as log:
        with pytest.raises(subprocess.TimeoutExpired):
            REAL_RUN_NOTIFY_COMMAND(command, {"PATH": os.environ["PATH"]}, log)
    time.sleep(2)
    assert not late.exists()                                                 # the grandchild was ended too
