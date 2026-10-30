"""Grok family integration; every HTTP response is fake and every login is disposable."""
import copy
import io
import json
import os
import time
import urllib.error

import pytest

from conftest import CONFIG, as_word
from quota_router import config, grok

GROK = CONFIG + '''
[[accounts]]
family = "grok"
key = "grok-1"
dir = ""
[[accounts]]
family = "grok"
key = "grok-2"
dir = "~/.grok-b"
'''
SECRET = "fake-grok-access-token-0123456789abcdef"
REFRESH = "fake-grok-refresh-token-0123456789"
EMAIL = "grok-owner@example.test"
# The billing answer as the CLI's endpoint gave it, unchanged.
PAYLOAD = {"config": {"currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY", "start": "2026-10-27T14:28:41.632158+00:00",
                                        "end": "2026-11-03T14:28:41.632158+00:00"},
                      "creditUsagePercent": 13.0, "onDemandCap": {"val": 0}, "onDemandUsed": {"val": 0},
                      "productUsage": [{"product": "GrokBuild", "usagePercent": 13.0}], "isUnifiedBillingUser": True,
                      "prepaidBalance": {"val": 0}, "topUpMethod": "TOP_UP_METHOD_SAVED_PAYMENT_METHOD",
                      "billingPeriodStart": "2026-10-27T14:28:41.632158+00:00",
                      "billingPeriodEnd": "2026-11-03T14:28:41.632158+00:00"}}


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime(ts))


def payload(used=13.0, start=None, end=None, kind="USAGE_PERIOD_TYPE_WEEKLY", cap=0, cap_used=0, prepaid=0):
    now = time.time()
    data = copy.deepcopy(PAYLOAD)
    data["config"].update(creditUsagePercent=used, onDemandCap={"val": cap}, onDemandUsed={"val": cap_used},
                          prepaidBalance={"val": prepaid})
    data["config"]["currentPeriod"] = {"type": kind, "start": iso(start or now - 2 * 86400), "end": iso(end or now + 5 * 86400)}
    return data


def setup(router, write_config, text=GROK):
    write_config(text)
    router.core.configure(config.load())
    return router.core


def store_login(acc, **fields):
    path = grok.auth_path(acc)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    entry = {"key": SECRET, "auth_mode": "oidc", "expires_at": iso(time.time() + 6 * 3600), "refresh_token": REFRESH,
             "oidc_issuer": "https://auth.x.ai", "oidc_client_id": "client", "email": EMAIL, "user_id": "user-1",
             "team_id": "team-1"}
    entry.update(fields)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"https://auth.x.ai::client": entry}, f)
    return entry


def fake_http(monkeypatch, core, handler=None):
    calls = []

    def open_url(req, timeout):
        calls.append(req)
        answer = handler(req) if handler else payload()
        return io.BytesIO(json.dumps(answer).encode())
    monkeypatch.setattr(core, "open_url", open_url)
    return calls


def secrets_absent(router):
    """Neither the token, the refresh token nor the email is in any state file."""
    for path in (router.home / "state").rglob("*"):
        if path.is_file():
            text = path.read_text(errors="replace")
            assert SECRET not in text and REFRESH not in text and EMAIL not in text, path.name


@pytest.fixture
def routed(router, write_config, monkeypatch):
    core = setup(router, write_config)
    for i, acc in enumerate(core.GROK_ACCOUNTS):
        router.usage[acc["key"]] = {"live": True, "fetched": core.now(), "credits": 0.0, "usage": {
            "seven_day": {"used": 40 - i * 20, "reset": core.now() + 5 * 86400, "start": core.now() - 2 * 86400,
                          "period": "weekly"}}}
    monkeypatch.setattr(core, "grok_reading", lambda acc, cache=None, force=False: copy.deepcopy(router.usage[acc["key"]]))
    return router


def decide(router, command):
    (router.home / "state" / "leases.json").unlink(missing_ok=True)
    return router.hook.decide(command, "/tmp", lead_key="claude-main")


# ---- parsing ---------------------------------------------------------------------
def test_parse_exact_api_fixture(router):
    usage, money = router.core.parse_grok_usage(PAYLOAD)
    week = usage["seven_day"]
    assert week["used"] == 13.0 and money == 0
    assert week["reset"] == router.core.iso_to_ts("2026-11-03T14:28:41.632158+00:00")
    assert week["start"] == router.core.iso_to_ts("2026-10-27T14:28:41.632158+00:00")
    assert week["period"] == "weekly" and set(usage) == {"seven_day"}


def test_period_that_is_not_weekly_keeps_its_own_start_and_end(router):
    start, end = time.time() - 10 * 86400, time.time() + 20 * 86400
    usage, _ = grok.parse_usage(payload(used=50, start=start, end=end, kind="USAGE_PERIOD_TYPE_MONTHLY"))
    assert usage["seven_day"]["period"] == "monthly"
    assert usage["seven_day"]["reset"] == pytest.approx(end, abs=1) and usage["seven_day"]["start"] == pytest.approx(start, abs=1)


def test_product_usage_is_the_fallback_percentage(router):
    data = copy.deepcopy(PAYLOAD)
    del data["config"]["creditUsagePercent"]
    data["config"]["productUsage"] = [{"product": "Other", "usagePercent": 90}, {"product": "GrokBuild", "usagePercent": 7}]
    assert grok.parse_usage(data)[0]["seven_day"]["used"] == 7


@pytest.mark.parametrize("data", [None, [], {}, {"config": []}, {"config": {}}, {"usage": 13},
                                  {"config": {"creditUsagePercent": "13", "currentPeriod": PAYLOAD["config"]["currentPeriod"]}},
                                  {"config": {"creditUsagePercent": -1, "currentPeriod": PAYLOAD["config"]["currentPeriod"]}},
                                  {"config": {"creditUsagePercent": float("nan"), "currentPeriod": PAYLOAD["config"]["currentPeriod"]}},
                                  {"config": {"creditUsagePercent": 13, "currentPeriod": {"end": "not a time"}}},
                                  {"config": {"creditUsagePercent": 13, "currentPeriod": {}}}])
def test_changed_formats_are_never_read_as_free_quota(router, data):
    assert grok.parse_usage(data) == ({}, 0.0)


def test_on_demand_headroom_and_prepaid_balance_count_as_money(router):
    assert grok.parse_usage(payload(cap=50, cap_used=10, prepaid=5))[1] == 45
    assert grok.parse_usage(payload(cap=50, cap_used=60))[1] == 0
    assert grok.parse_usage(payload(prepaid={"bad": 1}))[1] == 0


# ---- readings ----------------------------------------------------------------------
def test_reading_is_cached_and_sends_only_the_cli_headers(router, write_config, monkeypatch):
    core = setup(router, write_config)
    acc = core.GROK_ACCOUNTS[0]
    store_login(acc)
    calls = fake_http(monkeypatch, core)
    first = core.grok_reading(acc)
    assert first["live"] and first["usage"]["seven_day"]["used"] == 13
    assert core.grok_reading(acc)["usage"] == first["usage"] and len(calls) == 1
    req = calls[0]
    assert req.full_url == "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
    assert req.get_header("Authorization") == "Bearer " + SECRET
    assert req.get_header("X-grok-client-identifier") == "grok-shell"
    assert req.get_header("X-grok-client-version") == grok.CLIENT_VERSION
    assert core.observation_identity("grok", first) and "email" not in first
    secrets_absent(router)


def test_rotated_token_of_the_same_account_keeps_the_reading(router, write_config, monkeypatch):
    core = setup(router, write_config)
    acc = core.GROK_ACCOUNTS[0]
    store_login(acc)
    calls = fake_http(monkeypatch, core)
    core.grok_reading(acc)
    store_login(acc, key="rotated-token")
    assert core.grok_reading(acc)["live"] and len(calls) == 1


def test_another_account_logging_in_drops_the_old_numbers(router, write_config, monkeypatch):
    core = setup(router, write_config)
    acc = core.GROK_ACCOUNTS[0]
    store_login(acc)
    calls = fake_http(monkeypatch, core)
    core.grok_reading(acc)
    store_login(acc, user_id="user-2", key="other")
    assert core.grok_reading(acc)["live"] and len(calls) == 2 and calls[1].get_header("Authorization") == "Bearer other"


def test_expired_login_is_unknown_keeps_old_numbers_and_stays_dispatchable(router, write_config, monkeypatch):
    core = setup(router, write_config)
    acc = core.GROK_ACCOUNTS[0]
    store_login(acc)
    calls = fake_http(monkeypatch, core, lambda req: payload(used=30))
    core.grok_reading(acc)
    store_login(acc, expires_at=iso(time.time() - 60))
    result = core.grok_reading(acc, force=True)
    assert len(calls) == 1 and not result["live"] and result["usage"]["seven_day"]["used"] == 30
    row = core.find_row(core.score_rows("grok", core.read_family("grok")), "grok-1")
    assert row["wk_left"] is None and row["rate"] is None and not row["blocked"]
    assert any("numbers are old" in n and "70%" in n for n in row["notes"])
    assert any("expired" in n for n in row["notes"])
    best = core.choose("grok", core.score_rows("grok", core.read_family("grok")))[0]
    assert best is not None
    log = (router.home / "state" / "hook-errors.log").read_text()
    assert "grok-login-expired" in log and log.count("grok-login-expired") == 1
    secrets_absent(router)


def test_expired_login_never_starts_a_renewal_by_default(router, write_config, monkeypatch):
    core = setup(router, write_config)
    acc = core.GROK_ACCOUNTS[0]
    store_login(acc, expires_at=iso(time.time() - 60))
    started = []
    monkeypatch.setattr(grok, "spawn_refresh", lambda acc, owner: started.append(owner))
    calls = fake_http(monkeypatch, core)
    assert not core.grok_reading(acc).get("live")
    assert not started and not calls and core.GROK_REFRESH_LOGINS is False


@pytest.mark.parametrize("status", [401, 403, 500, 503])
def test_non_200_is_unknown_cools_down_and_logs_only_the_status(router, write_config, monkeypatch, status):
    core = setup(router, write_config)
    acc = core.GROK_ACCOUNTS[0]
    store_login(acc)

    def refuse(req):
        raise urllib.error.HTTPError(req.full_url, status, "secret body " + SECRET, {}, io.BytesIO(SECRET.encode()))
    calls = fake_http(monkeypatch, core, refuse)
    result = core.grok_reading(acc)
    assert not result["live"] and not result.get("usage") and result["retry_at"] > core.now()
    assert core.grok_reading(acc, force=True)["retry_at"] == result["retry_at"] and len(calls) == 1
    log = (router.home / "state" / "hook-errors.log").read_text()
    assert f"grok-query | HTTPError-{status}" in log and "secret body" not in log
    best = core.choose("grok", core.score_rows("grok", core.read_family("grok")))[0]
    assert best is not None
    secrets_absent(router)



def test_debug_mode_never_writes_the_query_error_text(router, write_config, monkeypatch):
    monkeypatch.setenv("QR_DEBUG", "1")
    core = setup(router, write_config)
    acc = core.GROK_ACCOUNTS[0]
    store_login(acc)

    def refuse(req):
        raise urllib.error.HTTPError(req.full_url, 500, "secret body " + SECRET, {}, io.BytesIO(SECRET.encode()))
    fake_http(monkeypatch, core, refuse)
    core.grok_reading(acc)
    assert "grok-query | HTTPError-500" in (router.home / "state" / "hook-errors.log").read_text()
    secrets_absent(router)

def test_changed_format_is_unknown(router, write_config, monkeypatch):
    core = setup(router, write_config)
    acc = core.GROK_ACCOUNTS[0]
    store_login(acc)
    fake_http(monkeypatch, core, lambda req: {"config": {"somethingElse": 1}})
    result = core.grok_reading(acc)
    assert not result["live"] and not result.get("usage") and "query failed" in result["error"]
    assert "grok-query | ValueError" in (router.home / "state" / "hook-errors.log").read_text()


def test_failed_query_after_a_good_one_keeps_the_old_numbers_marked_old(router, write_config, monkeypatch):
    core = setup(router, write_config)
    acc = core.GROK_ACCOUNTS[0]
    store_login(acc)
    answers = [payload(used=25)]

    def flaky(req):
        if answers:
            return answers.pop()
        raise OSError("offline")
    fake_http(monkeypatch, core, flaky)
    core.grok_reading(acc)
    result = core.grok_reading(acc, force=True)
    assert not result["live"] and result["usage"]["seven_day"]["used"] == 25
    row = core.find_row(core.score_rows("grok", core.read_family("grok")), "grok-1")
    assert row["wk_left"] is None and any("75%" in n for n in row["notes"])



def old_paid_reading(router, write_config, monkeypatch, used):
    """grok-1 read once (`used`%, with prepaid money), then every query fails: the reading is old."""
    core = setup(router, write_config)
    acc = core.GROK_ACCOUNTS[0]
    store_login(acc)
    answers = [payload(used=used, prepaid=20)]

    def flaky(req):
        if answers:
            return answers.pop()
        raise OSError("offline")
    fake_http(monkeypatch, core, flaky)
    core.grok_reading(acc)
    assert not core.grok_reading(acc, force=True)["live"]
    return core, acc


def test_old_reading_with_paid_money_is_held_without_consent(router, write_config, monkeypatch):
    # the period may have run out since the last good reading, and the money would then be charged.
    core, _ = old_paid_reading(router, write_config, monkeypatch, used=30)
    row = core.find_row(core.score_rows("grok", core.read_family("grok"), allow_credits=False), "grok-1")
    assert row["pay_risk"] and core.credit_block(row) and "not fresh" in row["blocked"]


def test_old_used_up_paid_reading_with_consent_is_weighted_down_and_marked(router, write_config, monkeypatch):
    core, _ = old_paid_reading(router, write_config, monkeypatch, used=100)
    rows = core.score_rows("grok", core.read_family("grok"), allow_credits=True)
    row = core.find_row(rows, "grok-1")
    assert not row["blocked"] and row["rate"] == 0.01 and row["will_spend"]
    assert any("purchased credits" in n for n in row["notes"])
    best = core.choose("grok", rows)[0]
    assert best["key"] == "grok-2"                  # an unknown account that charges nothing goes first
    core.log_decision("hook", "grok", "pick", row=row)
    assert "credits allowed" in (router.home / "state" / "decisions.log").read_text()


def test_logging_out_drops_the_old_paid_numbers(router, write_config, monkeypatch):
    core = setup(router, write_config)
    acc = core.GROK_ACCOUNTS[0]
    store_login(acc)
    fake_http(monkeypatch, core, lambda req: payload(used=95, prepaid=20))
    core.grok_reading(acc)
    os.remove(grok.auth_path(acc))
    result = core.grok_reading(acc, force=True)
    assert not result.get("usage") and not result.get("credits") and not result.get("live")
    row = core.find_row(core.score_rows("grok", core.read_family("grok"), allow_credits=False), "grok-1")
    assert not row["blocked"] and not row["paid"]

@pytest.mark.parametrize("contents", [None, "not JSON", "[]", '{"scope": {"auth_mode": "oidc"}}'])
def test_missing_login_or_grok_not_installed_is_unknown_but_dispatchable(router, write_config, monkeypatch, contents):
    core = setup(router, write_config)
    acc = core.GROK_ACCOUNTS[0]
    if contents is not None:
        os.makedirs(os.path.dirname(grok.auth_path(acc)), exist_ok=True)
        with open(grok.auth_path(acc), "w", encoding="utf-8") as f:
            f.write(contents)
    calls = fake_http(monkeypatch, core)
    result = core.grok_reading(acc)
    assert not calls and not result.get("live") and not result.get("no_login") and "auth.json" in result["error"]
    rows = core.score_rows("grok", core.read_family("grok"))
    assert all(not r["blocked"] for r in rows) and core.choose("grok", rows)[0] is not None


def test_no_time_left_in_the_hook_budget_uses_the_cache(router, write_config, monkeypatch):
    core = setup(router, write_config)
    acc = core.GROK_ACCOUNTS[0]
    store_login(acc)
    calls = fake_http(monkeypatch, core)
    first = core.grok_reading(acc)
    with core.budget(time.monotonic() + 0.5):
        assert core.grok_reading(acc, force=True)["usage"] == first["usage"]
    assert len(calls) == 1


def test_endpoint_slower_than_the_budget_is_unknown(router, write_config, monkeypatch):
    core = setup(router, write_config)
    acc = core.GROK_ACCOUNTS[0]
    store_login(acc)

    def slow(req, timeout):
        time.sleep(3)
        return io.BytesIO(json.dumps(PAYLOAD).encode())
    monkeypatch.setattr(core, "open_url", slow)
    started = time.monotonic()
    with core.budget(time.monotonic() + 1.5):
        result = core.grok_reading(acc)
    assert time.monotonic() - started < 2.8 and not result.get("live")


@pytest.mark.parametrize("url", ["http://cli-chat-proxy.grok.com/v1/billing", "https://evil.test/v1/billing",
                                 "https://cli-chat-proxy.grok.com.evil.test/x", "https://cli-chat-proxy.grok.com@evil.test/x",
                                 "https://u@cli-chat-proxy.grok.com/x", "https://cli-chat-proxy.grok.com:444/x",
                                 "https://cli-chat-proxy.grok.com/x#y"])
def test_billing_endpoint_rejects_other_destinations(home, url):
    with pytest.raises(config.ConfigError):
        config.parse_text('[endpoints]\ngrok_billing = ' + json.dumps(url) + '\n' + CONFIG)


# ---- renewal through the CLI (opt-in) ---------------------------------------------------
def test_opt_in_renewal_runs_grok_models_once_then_reads(router, write_config, monkeypatch):
    core = setup(router, write_config)
    monkeypatch.setattr(core, "GROK_REFRESH_LOGINS", True)
    acc = core.GROK_ACCOUNTS[1]
    store_login(acc, expires_at=iso(time.time() - 60))
    started, runs = [], []
    monkeypatch.setattr(grok, "spawn_refresh", lambda acc, owner: started.append(owner))

    def cli(argv, **kwargs):
        runs.append((argv, kwargs["env"].get("GROK_HOME")))
        store_login(acc, key="renewed", expires_at=iso(time.time() + 6 * 3600))
    monkeypatch.setattr(grok.subprocess, "run", cli)
    calls = fake_http(monkeypatch, core)
    first = core.grok_reading(acc)
    assert not first.get("live") and "grok models" in first["notes_extra"]
    core.grok_reading(acc, force=True)
    assert len(started) == 1 and not calls          # claimed once, nothing sent with the expired token
    assert grok.refresh_main([acc["key"], started[0]]) == 0
    assert runs == [(["grok", "models"], str(router.home / ".grok-b"))]
    assert calls[0].get_header("Authorization") == "Bearer renewed"
    assert core.grok_reading(acc)["live"]


def test_renewal_of_the_default_home_unsets_an_inherited_grok_home(router, write_config, monkeypatch):
    core = setup(router, write_config)
    monkeypatch.setenv("GROK_HOME", "/somewhere/else")
    acc = core.GROK_ACCOUNTS[0]
    store_login(acc, expires_at=iso(time.time() - 60))
    seen = []
    monkeypatch.setattr(grok.subprocess, "run", lambda argv, **kwargs: seen.append(kwargs["env"].get("GROK_HOME")))
    assert grok.renew_with_cli(acc) is False and seen == [None]



def test_renewal_on_windows_runs_the_cmd_shim_found_on_path(router, write_config, monkeypatch):
    # CreateProcess only adds .exe by itself; npm installs grok as grok.cmd.
    core = setup(router, write_config)
    acc = core.GROK_ACCOUNTS[0]
    store_login(acc, expires_at=iso(time.time() - 60))
    monkeypatch.setattr(grok.platform_compat, "system", lambda: "windows")
    monkeypatch.setattr(grok.shutil, "which", lambda name, path=None: "C:/npm/grok.cmd" if name == "grok" else None)
    seen = []
    monkeypatch.setattr(grok.subprocess, "run", lambda argv, **kwargs: seen.append(argv))
    grok.renew_with_cli(acc)
    assert seen == [["C:/npm/grok.cmd", "models"]]

@pytest.mark.parametrize("disable", ["config", "environment"])
def test_renewal_can_be_switched_off(router, write_config, monkeypatch, disable):
    core = setup(router, write_config)
    if disable == "config":
        monkeypatch.setattr(core, "GROK_REFRESH_LOGINS", False)
    else:
        monkeypatch.setattr(core, "GROK_REFRESH_LOGINS", True)
        monkeypatch.setenv("QR_NO_REFRESH", "1")
    acc = core.GROK_ACCOUNTS[0]
    store_login(acc, expires_at=iso(time.time() - 60))
    started = []
    monkeypatch.setattr(grok, "spawn_refresh", lambda acc, owner: started.append(owner))
    core.grok_reading(acc)
    assert not started


def test_failed_cli_renewal_is_unknown_and_cools_down(router, write_config, monkeypatch):
    core = setup(router, write_config)
    monkeypatch.setattr(core, "GROK_REFRESH_LOGINS", True)
    acc = core.GROK_ACCOUNTS[0]
    store_login(acc, expires_at=iso(time.time() - 60))
    started = []
    monkeypatch.setattr(grok, "spawn_refresh", lambda acc, owner: started.append(owner))

    def missing(*args, **kwargs):
        raise FileNotFoundError("grok")
    monkeypatch.setattr(grok.subprocess, "run", missing)
    calls = fake_http(monkeypatch, core)
    core.grok_reading(acc)
    assert grok.refresh_main([acc["key"], started[0]]) == 1
    result = core.grok_reading(acc, force=True)
    assert not calls and not result.get("live") and result["retry_at"] > core.now() and len(started) == 1
    log = (router.home / "state" / "hook-errors.log").read_text()
    assert "grok-renew | FileNotFoundError" in log
    secrets_absent(router)


def test_worker_without_the_claim_does_nothing(router, write_config, monkeypatch):
    core = setup(router, write_config)
    acc = core.GROK_ACCOUNTS[0]
    store_login(acc, expires_at=iso(time.time() - 60))
    monkeypatch.setattr(grok.subprocess, "run", lambda *a, **k: pytest.fail("ran grok without a claim"))
    assert grok.refresh_main([acc["key"], "lost-owner"]) == 1


# ---- the hook ---------------------------------------------------------------------
@pytest.mark.parametrize("command", ["grok -p hi", "grok --single hi", "grok --single=hi", "grok.exe -p hi",
                                     "/opt/homebrew/bin/grok -p hi", "grok --prompt-file task.md",
                                     "grok --always-approve --output-format json -p hi", "grok -phi"])
def test_one_shot_runs_are_routed_and_reserved(routed, command):
    result = decide(routed, command)
    assert "GROK_HOME=" + as_word(routed.home / ".grok-b") in result["command"]
    assert " -m " not in result["command"]          # no default_model configured: nothing added
    assert len(routed.core.active_leases()) == 1 and routed.core.active_leases()[0]["key"] == "grok-2"


@pytest.mark.parametrize("command", ["grok", 'grok "fix the bug"', "grok models", "grok login", "grok --help",
                                     "grok -v", "echo grok -p hi", "grok inspect", "grok -m -p", "grok agent stdio",
                                     "grok sessions -p"])
def test_sessions_subcommands_and_flag_values_are_not_dispatches(routed, command):
    assert decide(routed, command) is None


def test_default_model_is_added_when_configured(routed, monkeypatch):
    monkeypatch.setattr(routed.core, "GROK_MODEL", "grok-4.7")
    result = decide(routed, "grok -p hi")
    assert result["command"] == "env GROK_HOME=" + as_word(routed.home / ".grok-b") + " grok -m grok-4.7 -p hi"
    assert any("-m grok-4.7" in n for n in result["notes"])


@pytest.mark.parametrize("flags", ["-m custom -p hi", "--model=custom --single hi", "-p hi -m custom",
                                   "-p hi --model custom", "-p hi < /dev/null --model custom", "-mcustom -p hi",
                                   '-p "a prompt" -m custom'])
def test_written_model_is_preserved(routed, monkeypatch, flags):
    monkeypatch.setattr(routed.core, "GROK_MODEL", "grok-4.7")
    assert "grok-4.7" not in decide(routed, "grok " + flags)["command"]


def test_default_account_adds_no_home_prefix(routed):
    routed.usage["grok-2"]["usage"]["seven_day"]["used"] = 99
    assert decide(routed, "grok -p hi")["command"] is None
    assert routed.core.active_leases()[0]["key"] == "grok-1"
    assert routed.core.prefix("grok", {"dir": str(routed.home / ".grok")}) == ""


def test_explicit_and_inherited_grok_home_are_kept(routed, monkeypatch):
    command = "GROK_HOME=" + as_word(routed.home / ".grok") + " grok -p hi"
    assert decide(routed, command)["command"] is None
    assert routed.core.active_leases()[0]["key"] == "grok-1"
    monkeypatch.setenv("GROK_HOME", str(routed.home / ".grok"))
    assert decide(routed, "grok -p hi")["command"] is None
    assert routed.core.active_leases()[0]["key"] == "grok-1"


def make_session(home, folder, cwd, session):
    from urllib.parse import quote
    path = home / folder / "sessions" / quote(cwd, safe="") / session
    path.mkdir(parents=True)
    return path


def test_resume_by_id_keeps_the_account_that_holds_it(routed):
    session = "01a17c90-38c6-7ba2-b650-e3fa0d5d6b4b"
    make_session(routed.home, ".grok", "/elsewhere", session)
    for command in (f"grok -r {session} -p more", f"grok --resume={session} -p more", f"grok -p more --resume {session}"):
        result = decide(routed, command)
        assert result["command"] is None, command            # grok-1 sits at the default location: no prefix
        assert routed.core.active_leases()[0]["key"] == "grok-1"



def test_quoted_resume_id_attached_to_its_flag_keeps_the_account_that_holds_it(routed):
    session = "01a17c90-38c6-7ba2-b650-e3fa0d5d6b4b"
    make_session(routed.home, ".grok-b", "/elsewhere", session)
    routed.usage["grok-2"]["usage"]["seven_day"]["used"] = 90
    for command in (f'grok --resume="{session}" -p more', f"grok --resume='{session}' -p more", f'grok -r"{session}" -p more'):
        result = decide(routed, command)
        assert "GROK_HOME=" + as_word(routed.home / ".grok-b") in result["command"], command
        assert routed.core.active_leases()[0]["key"] == "grok-2"

def test_continue_uses_the_newest_conversation_for_this_directory(routed):
    old = make_session(routed.home, ".grok", "/tmp", "01a00000-0000-7000-8000-000000000001")
    make_session(routed.home, ".grok-b", "/tmp", "01a00000-0000-7000-8000-000000000002")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    for command in ("grok -c -p more", "grok --continue -p more", "grok -r -p more", "grok -p more -r"):
        result = decide(routed, command)
        assert "GROK_HOME=" + as_word(routed.home / ".grok-b") in result["command"], command


def test_resume_that_cannot_be_located_is_not_rerouted(routed):
    result = decide(routed, "grok -r 'my title' -p more")
    assert result["command"] is None and any("Grok" in n for n in result["notes"])
    assert routed.core.active_leases() == []


def test_used_up_period_without_paid_credits_is_not_dispatchable(routed):
    for key in ("grok-1", "grok-2"):
        routed.usage[key]["usage"]["seven_day"]["used"] = 100
    assert "weekly quota used up" in decide(routed, "grok -p hi")["deny"]


def test_paid_credits_need_explicit_consent(routed):
    for key in ("grok-1", "grok-2"):
        routed.usage[key]["usage"]["seven_day"]["used"] = 100
        routed.usage[key]["credits"] = 25.0
    denied = decide(routed, "grok -p hi")["deny"]
    assert "QR_ALLOW_CREDITS=1" in denied and "on-demand / prepaid" in denied
    allowed = decide(routed, "QR_ALLOW_CREDITS=1 grok -p hi")
    assert not allowed.get("deny") and any("purchased credits" in n for n in allowed["notes"])
    assert len(routed.core.active_leases()) == 1



@pytest.mark.parametrize("lang", ["en", "zh-TW"])
def test_refused_paid_grok_account_is_recorded_as_a_credit_refusal(routed, monkeypatch, lang):
    monkeypatch.setattr(routed.i18n, "_language", lang)
    routed.usage["grok-1"]["usage"]["seven_day"]["used"] = 100
    routed.usage["grok-1"]["credits"] = 25.0
    rows = routed.core.score_rows("grok", routed.core.read_family("grok"), allow_credits=False)
    row = routed.core.find_row(rows, "grok-1")
    assert row["will_spend"] and routed.core.decision_code(routed.core.credit_message(row)) == "credits"

def test_paid_account_near_the_floor_is_held_without_consent(routed):
    routed.usage["grok-1"]["usage"]["seven_day"]["used"] = 95
    routed.usage["grok-1"]["credits"] = 25.0
    routed.usage["grok-2"]["usage"]["seven_day"]["used"] = 100
    assert decide(routed, "grok -p hi").get("deny")


def test_cross_family_hint_and_summary_include_grok(routed):
    for key in ("claude-main", "claude-b"):
        routed.usage[key]["usage"]["seven_day"]["used"] = 85
    for key in ("codex-1", "codex-2"):
        routed.usage[key]["usage"]["seven_day"]["used"] = 90
    notes = "\n".join(decide(routed, "claude -p hi --model opus")["notes"])
    assert "grok -p" in notes and "Grok" in notes


def test_running_grok_jobs_use_grok_home(routed, monkeypatch):
    procs = {10: (1, "grok -p hi"), 11: (1, "grok.exe --model x --single hi"), 12: (1, "grok models"),
             13: (1, "grok"), 14: (10, "grok -p hi"), 15: (1, "grok leader serve")}
    monkeypatch.setattr(routed.core.platform_compat, "list_processes", lambda run: procs)
    monkeypatch.setattr(routed.core.platform_compat, "process_environment",
                        lambda pid, cmd, run: "GROK_HOME=" + str(routed.home / ".grok-b") if pid == 11 else "")
    assert routed.real_running_jobs() == {"grok-1": 1, "grok-2": 1}


def test_cli_commands_include_grok(routed, capsys):
    from quota_router import cli
    for bars in (False, True):
        routed.core.status(bars=bars)
        assert "grok-2" in capsys.readouterr().out.split("[grok]")[1]
    for argv in (["config"], ["pick", "grok"], ["check", "grok", "grok-1", "--peek"],
                 ["check", "grok", "2", "--peek"], ["decisions"], ["report"]):
        cli.main(argv)
    out = capsys.readouterr().out
    assert "grok-2" in out and "[families.grok]" in out and "grok_billing" in out and "GROK_HOME=" in out
    log = (routed.home / "state" / "decisions.log").read_text()
    assert "| grok | pick |" in log


def test_report_uses_the_period_start_and_length(routed, capsys):
    routed.usage["grok-1"]["usage"]["seven_day"].update(used=50, start=routed.core.now() - 10 * 86400,
                                                       reset=routed.core.now() + 20 * 86400, period="monthly")
    routed.core.report()
    line = next(line for line in capsys.readouterr().out.splitlines() if "grok-1" in line and "%" in line)
    assert "33.3% of this period has passed" in line


def test_status_names_a_period_that_is_not_weekly(routed, capsys):
    routed.usage["grok-1"]["usage"]["seven_day"]["period"] = "monthly"
    routed.core.status()
    assert "quota period: monthly" in capsys.readouterr().out.split("[grok]")[1]


def test_init_detects_grok_homes_without_reading_logins(home):
    (home / ".grok-b").mkdir()
    (home / ".grok-b" / "auth.json").write_text("not JSON")
    (home / ".grok-c").mkdir()                                    # no marker: not a Grok home
    found = [a for a in config.detect_accounts() if a["family"] == "grok"]
    assert found == [{"family": "grok", "key": "grok-1", "dir": ""}, {"family": "grok", "key": "grok-2", "dir": "~/.grok-b"}]


def test_default_model_setting_is_validated(home):
    assert config.parse_text('[families.grok]\ndefault_model = " grok-4.7 "\n' + CONFIG)["families.grok.default_model"] == "grok-4.7"
    with pytest.raises(config.ConfigError):
        config.parse_text('[families.grok]\ndefault_model = "x; rm -rf /"\n' + CONFIG)
