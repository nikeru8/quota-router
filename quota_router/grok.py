"""Grok Build CLI usage. The access token only leaves for the billing endpoint the CLI itself uses.

The login lives in $GROK_HOME/auth.json (default ~/.grok): one entry per OIDC scope, each with `key` (the access
token, about six hours long) and `expires_at`. This module never renews it: rotating the refresh token here could
sign the CLI out. An expired login is unknown quota with the last numbers kept and marked old. With
[families.grok] refresh_logins = true a detached worker runs `grok models`, which renews an expired login through
the CLI's own locked refresh, and then reads the quota again.
"""
import copy
import json
import os
import shutil
import subprocess
import urllib.request
import uuid

from . import config, core as qr, platform_compat
from .i18n import t

CLIENT_VERSION = "1.0.46"      # x-grok-client-version; the CLI it was read from
EXPIRY_MARGIN = 30             # a login this close to expiring counts as expired
QUERY_SECONDS = 90             # a background renewal (`grok models`, then the query) may take this long
MODELS_TIMEOUT = 60


def auth_path(acc):
    return os.path.join(acc["dir"] or config.default_dir("grok"), "auth.json")


def login(acc):
    """The first OIDC entry of auth.json that holds an access token, or {}."""
    try:
        with open(auth_path(acc), encoding="utf-8") as f:
            value = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    return next((entry for entry in value.values() if isinstance(entry, dict) and isinstance(entry.get("key"), str)
                 and entry["key"]), {})


def identity(data):
    """Which account the login belongs to, as a fingerprint; the token itself rotates every few hours."""
    user = data.get("user_id")
    return qr.fingerprint(user) if isinstance(user, str) else None


def timestamp(value):
    """An ISO time → epoch seconds; anything else (missing, malformed) → None."""
    try:
        return qr.iso_to_ts(value) if isinstance(value, str) and value else None
    except (TypeError, ValueError, OverflowError):
        return None


def expired(data):
    """Past expires_at (less a margin). No expiry at all is not expired: the CLI then assumes thirty days."""
    expiry = timestamp(data.get("expires_at"))
    return expiry is not None and expiry <= qr.now() + EXPIRY_MARGIN


def amount(value):
    """`{"val": 12}` → 12.0; anything else → 0."""
    number = qr.number(value.get("val")) if isinstance(value, dict) else None
    return number if number is not None and number > 0 else 0.0


def parse_usage(payload):
    """The billing answer → ({"seven_day": {used, reset, start, period}}, money that could be charged).

    The long window keeps the shared scoring key seven_day whatever its type; reset and start are the period's
    own end and start, so a period that is not seven days long is never treated as one. Money: what is left
    under the on-demand cap plus the prepaid balance; either is spent only once the period's credits are used up."""
    cfg = payload.get("config") if isinstance(payload, dict) else None
    if not isinstance(cfg, dict):
        return {}, 0.0
    used = qr.number(cfg.get("creditUsagePercent"))
    if used is None:
        product = next((p for p in cfg.get("productUsage") or [] if isinstance(p, dict) and p.get("product") == "GrokBuild"), {})
        used = qr.number(product.get("usagePercent"))
    period = cfg.get("currentPeriod") if isinstance(cfg.get("currentPeriod"), dict) else {}
    end = period.get("end") or cfg.get("billingPeriodEnd")
    start = period.get("start") or cfg.get("billingPeriodStart")
    reset = timestamp(end)
    if used is None or used < 0 or reset is None:
        return {}, 0.0
    kind = period.get("type") if isinstance(period.get("type"), str) else ""
    window = {"used": min(100.0, used), "reset": reset,
              "start": timestamp(start),
              "period": kind.removeprefix("USAGE_PERIOD_TYPE_").lower() or None}
    money = max(0.0, amount(cfg.get("onDemandCap")) - amount(cfg.get("onDemandUsed"))) + amount(cfg.get("prepaidBalance"))
    return {"seven_day": window}, money


def request_json(url, token):
    config.validate_grok_endpoint(url)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token, "Accept": "application/json", "User-Agent": qr.USER_AGENT,
        "x-grok-client-identifier": "grok-shell", "x-grok-client-version": CLIENT_VERSION})
    return qr.network_json(req)  # NoRedirect, bounded by the caller's budget


def refresh_enabled():
    return qr.GROK_REFRESH_LOGINS and os.environ.get("QR_NO_REFRESH") != "1"


def spawn_refresh(acc, owner):
    with open(os.path.join(qr.STATE, f"refresh-{acc['key']}.log"), "a", encoding="utf-8") as log:
        return platform_compat.spawn_detached(
            [qr.sys.executable, "-I", "-X", "utf8", qr.ENTRY_FILE, "refresh-grok", acc["key"], owner],
            env=dict(os.environ), stdout=log, stderr=log)


def reading(acc, cache=None, force=False):
    cache = {} if cache is None else cache
    key, data = acc["key"], login(acc)
    owner = uuid.uuid4().hex
    claimed = renewing = False
    with qr.state_lock("cache") as held:
        latest = qr.load("cache.json", {})
        c = copy.deepcopy(latest.get(key, cache.get(key, {})))
        if "fetched" in c:
            c["fetched"] = qr.fetched(c)
        sig = identity(data)
        if sig and c.get("account_sig") and sig != c["account_sig"]:
            # another account logged in here: the old account's numbers do not describe it.
            for field in ("usage", "fetched", "credits", "live", "error", "retry_at", "failures", "notes_extra",
                          "inflight_until", "inflight_owner", "reset_checked"):
                c.pop(field, None)
            c["token_changed_at"] = qr.now()
        if sig:
            c["account_sig"] = sig
        idle = held and c.get("retry_at", 0) <= qr.now() and c.get("inflight_until", 0) <= qr.now()
        if not data:
            # no login file (or grok is not installed): unknown, and still dispatchable, as with XAI_API_KEY.
            # The old numbers and money no longer describe anyone, so they cannot hold the account back either.
            for field in ("usage", "fetched", "credits"):
                c.pop(field, None)
            c.update(live=False, error=t("err.grok_no_login"))
        elif expired(data):
            if c.get("error") != t("err.grok_expired"):
                qr.log_error("grok-login-expired", subject=key)
            c.update(live=False, error=t("err.grok_expired"))
            if idle and refresh_enabled() and qr.remaining() >= 1:
                c.update(inflight_owner=owner, inflight_until=qr.now() + QUERY_SECONDS)
                claimed = renewing = True
        elif idle and qr.remaining() >= 1 and (force or not c.get("usage") or not c.get("live")
                                                or qr.now() - qr.fetched(c) >= qr.USAGE_CACHE_SEC or qr.reset_due(c)):
            c.update(inflight_owner=owner, inflight_until=qr.now() + QUERY_SECONDS)
            claimed = True
        if held and c != latest.get(key):
            latest[key] = c
            qr.save("cache.json", latest)
    if not claimed:
        cache[key] = c
        return c
    if renewing:
        try:
            spawn_refresh(acc, owner)
            c = dict(c, notes_extra=t("note.grok_refresh_started"))
        except Exception as error:
            failed = dict(c)
            qr.cooldown(failed, error)
            c = qr.merge_cache(key, c, failed, owner, strict=True)
            qr.log_error("grok-refresh-start", error, key, trace=False)
    else:
        c = query(acc, owner)
    cache[key] = c
    return c


def query(acc, owner):
    """One billing query for the claim `owner` holds. A failure keeps the last numbers (marked old when scored)."""
    key = acc["key"]
    before = qr.load("cache.json", {}).get(key, {})
    if before.get("inflight_owner") != owner:
        return before
    c, data = copy.deepcopy(before), login(acc)
    sig = identity(data)
    success = False
    try:
        if not data:
            raise ValueError("no login")
        if expired(data):
            raise ValueError("login expired")
        payload = request_json(qr.ENDPOINTS["grok_billing"], data["key"])
        usage, money = parse_usage(payload)
        if not usage:
            raise ValueError("no Grok quota window")
        c.update(usage=usage, credits=money, fetched=qr.now(), live=True)
        for field in ("error", "retry_at", "failures", "notes_extra"):
            c.pop(field, None)
        if sig:
            c["account_sig"] = sig
        success = True
    except Exception as error:
        c.update(live=False, error=t("err.grok_no_login") if not data else
                 t("err.grok_expired") if expired(data) else t("err.grok_usage"))
        c.pop("notes_extra", None)
        if not data:
            for field in ("usage", "fetched", "credits"):
                c.pop(field, None)
        qr.cooldown(c, error)
        # the class and an HTTP status only; never a message, a body or a header, not even in debug.log.
        qr.log_error("grok-query", error, key, trace=False)
    if identity(login(acc)) != sig:
        c = dict(before, live=False, error=t("err.grok_changed"))
        success = False
    c = qr.merge_cache(key, before, c, owner, strict=True)
    if success and c.get("live"):
        qr.quiet_call(qr.observe_reading, "grok", key, c)
    return c


def renew_with_cli(acc):
    """Run `grok models` against this account's home. It costs no tokens; the CLI refreshes an expired login
    under its own auth.json lock and writes it back. True when the login is no longer expired afterwards."""
    env = dict(os.environ)
    if platform_compat.canonical(acc["dir"]) == platform_compat.canonical(config.default_dir("grok")):
        env.pop("GROK_HOME", None)
    else:
        env["GROK_HOME"] = acc["dir"]
    program = qr.GROK_PROGRAM
    if platform_compat.system() == "windows":
        # CreateProcess only adds .exe by itself, and npm installs grok as a .cmd shim.
        program = shutil.which(program, path=env.get("PATH")) or program
    subprocess.run([program, "models"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, cwd=qr.STATE, env=env, timeout=MODELS_TIMEOUT, check=False)
    data = login(acc)
    return bool(data) and not expired(data)


def refresh_main(argv):
    """`qr refresh-grok <key> <owner>`: the background renewal reading() started. Not a user command."""
    acc = next((a for a in qr.GROK_ACCOUNTS if len(argv) == 2 and a["key"] == argv[0]), None)
    if acc is None:
        return 2
    try:
        if qr.load("cache.json", {}).get(acc["key"], {}).get("inflight_owner") != argv[1]:
            outcome = "lost"
        else:
            try:
                renew_with_cli(acc)
            except Exception as error:   # grok missing, or it hung: the query below records the failure
                qr.log_error("grok-renew", error, acc["key"], trace=False)
            outcome = "ok" if query(acc, argv[1]).get("live") else "failed"
    except Exception as error:
        qr.log_error("grok-refresh", error, acc["key"], trace=False)
        outcome = "failed"
    print(outcome, flush=True)
    return 0 if outcome == "ok" else 1
