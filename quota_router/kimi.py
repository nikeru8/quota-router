"""Kimi Code usage and OAuth renewal. Credentials only leave for exact Kimi HTTPS hosts.

The CLI locks oauth/kimi-code.lock with proper-lockfile (5 s stale, heartbeat).
Take that lock and a router lock per credential path, then compare + temp + replace.
A hook delegates an expiring login to a detached worker so its watchdog cannot
interrupt token rotation. Ordinary CLI reads complete the exchange in place.
"""
import copy
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import config, core as qr, platform_compat
from .i18n import t

CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
REFRESH_MARGIN = 30
QUERY_SECONDS = 120


def credential_path(acc):
    return os.path.join(acc["dir"] or config.default_dir("kimi"), "credentials", "kimi-code.json")


def login(acc):
    try:
        with open(credential_path(acc), encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def token_sig(data):
    return qr.fingerprint(data.get("access_token"))


def expires_soon(data):
    expiry = qr.number(data.get("expires_at"))
    return not data.get("access_token") or expiry is not None and expiry <= qr.now() + REFRESH_MARGIN


def parse_usage(payload):
    """Keep the shared scoring key seven_day for Kimi's MONTH, plus its code-only month."""
    usage = {}
    raw = payload.get("usages") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return usage
    for source, key in (("limit_5h", "five_hour"), ("limit_month_total", "seven_day"),
                        ("limit_month_code", "month_code")):
        window = raw.get(source)
        if not isinstance(window, dict):
            continue
        ratio = qr.number(window.get("used_ratio"))
        if ratio is None or not 0 <= ratio <= 1:
            continue
        usage[key] = {"used": ratio * 100, "reset": qr.iso_to_ts(window.get("reset_time"))}
    return usage


def request_json(url, token=None, body=None):
    config.validate_kimi_endpoint(url, token=body is not None)
    headers = {"Accept": "application/json", "User-Agent": qr.USER_AGENT}
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=headers)
    if body is None:
        return qr.network_json(req)  # NoRedirect, bounded by the caller's budget
    # Only called outside a hook's watchdog, with interruption_shield held.
    with qr.open_url(req, timeout=15) as response:
        return json.load(response)


def token_urls():
    host = os.environ.get("KIMI_CODE_OAUTH_HOST") or os.environ.get("KIMI_OAUTH_HOST")
    primary = host.rstrip("/") + "/api/oauth/token" if host else qr.ENDPOINTS["kimi_token"]
    urls = list(dict.fromkeys((primary, qr.ENDPOINTS["kimi_token_fallback"])))
    # Validate every candidate before sending anything, including environment overrides.
    return [config.validate_kimi_endpoint(url, token=True) for url in urls]


def save_login(acc, old, answer):
    """Preserve CLI fields; never overwrite a concurrently changed access/refresh token."""
    if not isinstance(answer, dict):
        raise ValueError("invalid token response")
    current = login(acc)
    if any(current.get(key) != old.get(key) for key in ("access_token", "refresh_token")):
        raise ValueError("login changed during renewal")
    fields = {}
    for key in ("access_token", "refresh_token", "token_type", "scope"):
        value = answer.get(key)
        if isinstance(value, str) and value:
            fields[key] = value
    life = qr.number(answer.get("expires_in"))
    complete = bool(fields.get("access_token") and life is not None and 0 < life < 10**9)
    if complete:
        fields.update(expires_in=answer["expires_in"], expires_at=int(qr.now() + life))
    else:
        # Preserve a rotated refresh token even if the rest of the answer is malformed.
        fields = {"refresh_token": fields["refresh_token"]} if fields.get("refresh_token") else {}
    if not fields:
        raise ValueError("token response has no usable credentials")
    updated = dict(current, **fields)
    path = credential_path(acc)
    fd, temp = tempfile.mkstemp(prefix=".kimi-code-", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(updated, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        if login(acc) != current:
            raise ValueError("login changed before save")
        platform_compat.replace(temp, path)
        if login(acc) != updated:
            raise OSError("login write could not be verified")
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    if not complete:
        raise ValueError("incomplete token response; rotated refresh token preserved")
    return updated


@contextmanager
def oauth_lock(acc):
    """Cooperate with Kimi Code 2.3.0's proper-lockfile; never touch its sentinel contents.

    Windows Kimi disables this lock. The router's own path lock still serializes
    its renewals there, and credential comparisons protect against CLI changes.
    """
    if platform_compat.system() == "windows":
        yield lambda: None
        return
    path = os.path.join(acc["dir"] or config.default_dir("kimi"), "oauth", "kimi-code.lock")
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        stat = os.stat(path)
        if time.time() - stat.st_mtime <= 5:
            raise ValueError("Kimi CLI is renewing its login")
        # Same stale-lock recovery as proper-lockfile; never remove a refreshed lock.
        if os.stat(path).st_mtime_ns != stat.st_mtime_ns:
            raise ValueError("Kimi lock was refreshed")
        os.rmdir(path)
        os.mkdir(path, 0o700)
    identity = os.stat(path).st_ino
    stop, lost = threading.Event(), threading.Event()

    def check():
        if lost.is_set() or os.stat(path).st_ino != identity:
            raise OSError("Kimi renewal lock lost")

    def heartbeat():
        while not stop.wait(1):
            try:
                check()
                os.utime(path, None)
            except OSError:
                lost.set()
                return

    thread = threading.Thread(target=heartbeat, name="kimi-oauth-lock", daemon=True)
    thread.start()
    try:
        yield check
    finally:
        stop.set()
        thread.join(1)
        try:
            if not lost.is_set() and os.stat(path).st_ino == identity:
                os.rmdir(path)
        except OSError:
            pass


def renew(acc, old):
    if not qr.KIMI_REFRESH_LOGINS or os.environ.get("QR_NO_REFRESH") == "1":
        raise ValueError("Kimi renewal disabled")
    if not isinstance(old.get("refresh_token"), str) or not old["refresh_token"]:
        raise ValueError("no refresh token")
    urls = token_urls()
    lock = "kimi-login-" + qr.fingerprint(os.path.realpath(credential_path(acc)))
    with qr.state_lock(lock, timeout=0) as held:
        if not held:
            raise ValueError("Kimi renewal already running")
        with oauth_lock(acc) as check_lock:
            current = login(acc)
            if current != old:
                if not expires_soon(current):
                    return current
                raise ValueError("Kimi login changed")
            body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": old["refresh_token"],
                                           "client_id": CLIENT_ID}).encode()
            with qr.interruption_shield():
                for i, url in enumerate(urls):
                    try:
                        answer = request_json(url, body=body)
                    except (OSError, ValueError) as error:
                        if i + 1 == len(urls):
                            raise
                        # A rejected or unreachable regional host may fall back; redirects are never followed.
                        if isinstance(error, urllib.error.HTTPError) and 300 <= error.code < 400:
                            raise
                        continue
                    # Never retry a successful POST if validation or saving its answer fails.
                    check_lock()
                    return save_login(acc, old, answer)


def spawn_refresh(acc, owner):
    with open(os.path.join(qr.STATE, f"refresh-{acc['key']}.log"), "a", encoding="utf-8") as log:
        return platform_compat.spawn_detached(
            [qr.sys.executable, "-I", "-X", "utf8", qr.ENTRY_FILE, "refresh-kimi", acc["key"], owner],
            env=dict(os.environ), stdout=log, stderr=log)


def reading(acc, cache=None, force=False):
    cache = {} if cache is None else cache
    key, data = acc["key"], login(acc)
    sig = token_sig(data)
    owner = uuid.uuid4().hex
    with qr.state_lock("cache") as held:
        latest = qr.load("cache.json", {})
        c = copy.deepcopy(latest.get(key, cache.get(key, {})))
        if c.get("token_sig") != sig:
            c = {"token_sig": sig}
        if not data.get("access_token") and not data.get("refresh_token"):
            c.update(usage=None, live=False, no_login=True, error=t("err.no_login"))
        elif (held and qr.remaining() >= 1 and c.get("retry_at", 0) <= qr.now()
              and c.get("inflight_until", 0) <= qr.now()
              and (force or not c.get("usage") or qr.now() - qr.fetched(c) >= qr.USAGE_CACHE_SEC or qr.reset_due(c))):
            if expires_soon(data) and qr.DEADLINE.get() is not None:
                c.update(usage=None, live=False)
            c.update(inflight_owner=owner, inflight_until=qr.now() + QUERY_SECONDS)
        if held:
            latest[key] = c
            qr.save("cache.json", latest)
    if c.get("inflight_owner") != owner:
        cache[key] = c
        return c
    if expires_soon(data) and qr.DEADLINE.get() is not None and qr.KIMI_REFRESH_LOGINS and os.environ.get("QR_NO_REFRESH") != "1":
        try:
            spawn_refresh(acc, owner)
            c = dict(c, usage=None, live=False, notes_extra=t("note.kimi_refresh_started"))
        except Exception as error:
            failed = dict(c, usage=None, live=False, error=t("err.kimi_refresh"))
            qr.cooldown(failed, error)
            c = qr.merge_cache(key, c, failed, owner, strict=True)
            qr.log_error("kimi-refresh-start", subject=key)
    else:
        c = query(acc, owner)
    cache[key] = c
    return c


def query(acc, owner):
    key = acc["key"]
    before = qr.load("cache.json", {}).get(key, {})
    if before.get("inflight_owner") != owner:
        return before
    c, data = copy.deepcopy(before), login(acc)
    sig = token_sig(data)
    if sig != before.get("token_sig"):
        return qr.merge_cache(key, before, dict(before, usage=None, live=False), owner, strict=True)
    success = False
    try:
        if expires_soon(data):
            data = renew(acc, data)
            sig = token_sig(data)
            c["token_sig"] = sig
        if not data.get("access_token"):
            raise ValueError("no access token")
        payload = request_json(qr.ENDPOINTS["kimi_usage"], token=data["access_token"])
        usage = parse_usage(payload)
        if not usage.get("seven_day") and not usage.get("five_hour"):
            raise ValueError("no Kimi quota windows")
        c.update(usage=usage, fetched=qr.now(), live=True, token_sig=sig)
        for field in ("error", "retry_at", "failures", "no_login", "notes_extra"):
            c.pop(field, None)
        if not c.get("email") or qr.now() - c.get("profile_at", 0) >= qr.PROFILE_CACHE_SEC:
            try:
                profile = request_json(qr.ENDPOINTS["kimi_profile"], token=data["access_token"])
                if isinstance(profile.get("email"), str) and profile["email"]:
                    c.update(email=profile["email"], profile_at=qr.now())
                if isinstance(profile.get("user_level_name"), str):
                    c["plan"] = profile["user_level_name"]
            except Exception:
                pass  # identity lookup must not discard a successful quota reading
        c["account_sig"] = qr.fingerprint(c.get("email")) or sig
        success = True
    except Exception as error:
        # Renewal failure means unknown, not a logged-out directory. It remains dispatchable.
        c.update(usage=None, live=False, error=t("err.kimi_refresh") if expires_soon(data) else t("err.kimi_usage"))
        c.pop("no_login", None)
        qr.cooldown(c, error)
        # Do not pass response bodies or token-bearing exception messages to even the debug log.
        qr.log_error("kimi-query", subject=key)
    if token_sig(login(acc)) != sig:
        c = dict(before, usage=None, live=False, error=t("err.kimi_changed"))
        success = False
    c = qr.merge_cache(key, before, c, owner, strict=True)
    if success and c.get("live"):
        qr.quiet_call(qr.observe_reading, "kimi", key, c)
    return c


def refresh_main(argv):
    acc = next((a for a in qr.KIMI_ACCOUNTS if len(argv) == 2 and a["key"] == argv[0]), None)
    if acc is None:
        return 2
    try:
        outcome = "ok" if query(acc, argv[1]).get("live") else "failed"
    except Exception:
        qr.log_error("kimi-refresh", subject=acc["key"])
        outcome = "failed"
    print(outcome, flush=True)
    return 0 if outcome == "ok" else 1
