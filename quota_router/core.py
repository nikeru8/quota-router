"""Quota readings, scoring and account selection, plus the qr subcommands. The usage text is the i18n key "help"."""
import json, os, re, subprocess, sys, time, urllib.request, urllib.error, statistics, hashlib, base64
import uuid, copy, math, shlex, traceback
from contextlib import contextmanager
from contextvars import ContextVar
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

from . import __version__, config, i18n, platform_compat
from .i18n import t

HOME = os.path.expanduser("~")
STATE = config.state_dir()[0]

# ---- settings: these are the defaults; configure() fills in the config file's values ----
CLAUDE_ACCOUNTS = []           # {"key", "dir"}; dir=None is the default location (CLAUDE_CONFIG_DIR unset)
CODEX_ACCOUNTS = []
AGY_ACCOUNTS = []              # at most one: agy has no directory variable, so only its default location exists
DEFAULT_KEY = {"claude": None, "codex": None, "agy": None}   # the account of each family that uses the default location
LAUNCH_OFF = set()             # Claude account keys with `launch = false` (no new sessions from `qr launch`)
USAGE_CACHE_SEC = 180          # a usage reading is cached for 3 minutes
PROFILE_CACHE_SEC = 86400      # the identity (email) is cached for a day; a changed token re-queries at once
LEASE_SEC = 30 * 60            # a reservation expires after 30 minutes
LEASE_BUSY_SEC = 300           # and only counts as busy for its first five minutes
HOOK_BUDGET_SEC = 8
DEADLINE = ContextVar("quota_deadline", default=None)
FIVE_HOUR_FLOOR = 10           # skip an account whose five-hour window is under 10%
LEAD_FLOOR = 20                # the lead's account takes no subagents under 20% weekly
LEAD_FH_FLOOR = 50             # ...nor when its five-hour window (after running jobs) is under 50%
LEAD_LAST_RESORT = False       # true: the lead's account is picked only when no other account can take the job
INFLIGHT_FH_COST = 3           # each running job is assumed to still take this many % of the five-hour window
CODEX_STALE_H = 2              # non-live Codex numbers older than 2 hours: unknown, ranked in the middle, flagged "may have reset"
CODEX_PROBE_H = 2              # live Codex query failed and the local log is over 2 hours old → one background no-op probe to refresh it
TIGHT_WARN = 15                # warn when the best account has under 15% weekly left
SUB_FLOOR = 10                 # non-lead accounts under 10% weekly are skipped; paid ones need consent for this dispatch
CROSS_HINT_MINE = 30           # start looking at the other family when this one's best weekly is under 30%
CROSS_HINT_OTHER = 50          # hint when the other family's best weekly is ≥50% or at least double (a hint only; the family is never switched)
CROSS_IDLE_OTHER = 90          # second trigger: the other family has an idle account at ≥90% while the one picked here has ≥CROSS_BUSY_MINE jobs or at most half that quota
CROSS_BUSY_MINE = 3
NOTICE_TTL = 6 * 3600          # how long a reset notice stays valid
RESET_DROP = 3                 # usage must drop this many points to count as a reset / refill
ALLOW_CREDITS = False          # True: purchased credits may be spent without the QR_ALLOW_CREDITS=1 prefix
SCARCE = "fable"               # the model with its own weekly bucket; None when there is none
SCARCE_LABEL = "Fable"
AGY_MODEL = ""                 # --model added to an `agy -p` dispatch that names none ("" = leave it alone)
AGY_PROGRAM = "agy"            # the Antigravity CLI that is asked for its quota (`agy -p /usage`)
LABELS = {"claude": "Claude", "codex": "Codex", "agy": "agy"}
VERBS = {"claude": "claude -p", "codex": "codex exec", "agy": "agy -p"}
REFRESH_LOGINS = {"claude": False, "codex": False}   # per family, true: renew an expiring login with its refresh token (QR_NO_REFRESH=1: never)
CODEX_REFRESH_BEFORE_SEC = 72 * 3600   # a Codex login is renewed once its access token expires within this long
NOTIFY_COMMAND = ""            # [notify] on_refresh_failed: shell command run when a login renewal fails ("" = none)
NOTIFY_REPEAT_SEC = 12 * 3600  # the same account and reason runs it at most once in this long
NOTIFY_FAILED_ATTEMPTS = 2     # a temporary failure ("failed") runs it only after this many in a row
# where the login tokens are sent; the config file can point these elsewhere for tests or a proxy.
ENDPOINTS = {name.split(".", 1)[1]: url for name, url in config.DEFAULTS.items() if name.startswith("endpoints.")}


def configure(cfg):
    """Apply a loaded config to this module. Every entry point (qr, the hook, tests) calls this once before doing anything."""
    global HOME, STATE
    HOME = os.path.expanduser("~")
    STATE = config.state_dir()[0]
    try:
        os.makedirs(STATE, exist_ok=True)
    except OSError:
        pass  # an unwritable state directory still allows this one decision, in memory.
    i18n.set_language(i18n.resolve(cfg["general.language"]))
    scarce = cfg["families.claude.scarce_model"] or None
    globals().update(
        CLAUDE_ACCOUNTS=[{"key": a["key"], "dir": a["dir"]} for a in cfg.family("claude")],
        CODEX_ACCOUNTS=[{"key": a["key"], "dir": a["dir"]} for a in cfg.family("codex")],
        AGY_ACCOUNTS=[{"key": a["key"], "dir": a["dir"]} for a in cfg.family("agy")],
        AGY_MODEL=cfg["families.agy.default_model"],
        LAUNCH_OFF={a["key"] for a in cfg.family("claude") if not a["launch"]},
        DEFAULT_KEY={family: next((a["key"] for a in cfg.family(family) if a["default"]), None)
                     for family in config.FAMILIES},
        USAGE_CACHE_SEC=cfg["timing.usage_cache_seconds"],
        PROFILE_CACHE_SEC=cfg["timing.profile_cache_seconds"],
        LEASE_SEC=cfg["timing.lease_seconds"],
        LEASE_BUSY_SEC=cfg["timing.busy_window_seconds"],
        HOOK_BUDGET_SEC=cfg["timing.hook_budget_seconds"],
        FIVE_HOUR_FLOOR=cfg["limits.five_hour_floor"],
        LEAD_FLOOR=cfg["general.protect_threshold"],
        LEAD_FH_FLOOR=cfg["general.protect_five_hour"],
        LEAD_LAST_RESORT=cfg["general.lead_last_resort"],
        INFLIGHT_FH_COST=cfg["limits.inflight_five_hour_cost"],
        CODEX_STALE_H=cfg["timing.codex_stale_hours"],
        CODEX_PROBE_H=cfg["timing.codex_probe_hours"],
        TIGHT_WARN=cfg["limits.tight_warning"],
        SUB_FLOOR=cfg["limits.weekly_floor"],
        CROSS_HINT_MINE=cfg["hints.home_low"],
        CROSS_HINT_OTHER=cfg["hints.other_high"],
        CROSS_IDLE_OTHER=cfg["hints.other_idle"],
        CROSS_BUSY_MINE=cfg["hints.home_busy"],
        NOTICE_TTL=cfg["timing.notice_ttl_hours"] * 3600,
        RESET_DROP=cfg["events.reset_drop_points"],
        ALLOW_CREDITS=cfg["general.allow_credits"],
        REFRESH_LOGINS={"claude": cfg["families.claude.refresh_logins"], "codex": cfg["families.codex.refresh_logins"]},
        CODEX_REFRESH_BEFORE_SEC=cfg["families.codex.refresh_before_hours"] * 3600,
        NOTIFY_COMMAND=cfg["notify.on_refresh_failed"].strip(),
        NOTIFY_REPEAT_SEC=cfg["notify.repeat_hours"] * 3600,
        NOTIFY_FAILED_ATTEMPTS=cfg["notify.failed_attempts"],
        ENDPOINTS={name: cfg["endpoints." + name] for name in ENDPOINTS},
        SCARCE=scarce, SCARCE_LABEL=scarce.capitalize() if scarce else "")


def accounts(family):
    return {"claude": CLAUDE_ACCOUNTS, "codex": CODEX_ACCOUNTS, "agy": AGY_ACCOUNTS}.get(family, [])


def managed_families():
    """The families that have at least one account in the config file, in a fixed order."""
    return [family for family in config.FAMILIES if accounts(family)]


def other_families(family):
    return [other for other in managed_families() if other != family]


def windows():
    """Names of the quota windows that are tracked; the per-model window is keyed by the model name."""
    return ("seven_day", SCARCE, "five_hour") if SCARCE else ("seven_day", "five_hour")


def weekly_windows(family):
    return ("seven_day", SCARCE) if family == "claude" and SCARCE else ("seven_day",)


def now():
    return time.time()


def load(name, default):
    try:
        with open(os.path.join(STATE, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save(name, data):
    p = os.path.join(STATE, name)
    tmp = f"{p}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "x", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        platform_compat.replace(tmp, p)
        return True
    except OSError as e:
        log_error("state-save", e, name)
        return False
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def remaining():
    deadline = DEADLINE.get()
    return max(0, deadline - time.monotonic()) if deadline is not None else float("inf")


@contextmanager
def budget(deadline):
    token = DEADLINE.set(deadline)
    try:
        yield
    finally:
        DEADLINE.reset(token)


SECRET_PATTERNS = (
    (re.compile(r"(?i)\b(bearer|basic)(\s+)[^\s'\"\\]+"), r"\1\2[redacted]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{6,}"), "[redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]+)*"), "[redacted]"),
    # NAME=<long value>; a value that starts like a path is a directory, not a secret.
    (re.compile(r"(=\s*[bB]?['\"]?)(?![/~.\\]|[A-Za-z]:[\\/])[^\s'\"&;,)]{20,}"), r"\1[redacted]"),
)


def redact(text):
    """Blank out what looks like a credential. Best effort, pattern-based; used for the QR_DEBUG file and for the
    shell startup lines `qr doctor` quotes."""
    text = str(text)
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def log_token(value):
    """A value as it may be written to a log file: letters, digits, `_`, `.` and `-` only. Anything else (an
    account key with other characters, a session id of an unexpected shape) is recorded as a hash of itself."""
    text = "" if value is None else str(value)
    if not text:
        return "-"
    return text if LOG_TOKEN.fullmatch(text) else "#" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


def error_location(error):
    """`file:line:function` of the innermost frame of this package in the exception's traceback, or `-`."""
    found = "-"
    tb = getattr(error, "__traceback__", None)
    while tb is not None:
        code = tb.tb_frame.f_code
        if os.path.normcase(os.path.dirname(os.path.abspath(code.co_filename))) == os.path.normcase(PACKAGE_DIR):
            found = f"{os.path.basename(code.co_filename)}:{tb.tb_lineno}:{code.co_name.strip('<>')}"
        tb = tb.tb_next
    return found


def log_error(operation, error=None, subject=None):
    """One structured line in hook-errors.log: time | operation | exception class | code location | subject.

    Nothing else is ever written there. In particular not the exception's message and not a traceback: both
    can quote request headers, response bodies, config values or the command. The one exception is the status
    code of an HTTP error answer, appended to its class (`HTTPError-403`): a number, and without it a refusal
    could not be told from an outage. `operation` is a fixed name from the code and `subject` a state file, lock
    or account key, hashed when it has unusual characters.
    QR_DEBUG=1 additionally appends the full traceback to debug.log, which may therefore hold sensitive text."""
    try:
        kind = log_token(type(error).__name__) if error is not None else "-"
        status = getattr(error, "code", None) if isinstance(error, urllib.error.HTTPError) else None
        if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 999:
            kind += f"-{status}"
        with open(os.path.join(STATE, "hook-errors.log"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {log_token(operation)} | {kind} | "
                    f"{error_location(error)} | {log_token(subject)}\n")
    except Exception:
        pass
    if error is not None and os.environ.get("QR_DEBUG") == "1":
        try:
            detail = "".join(traceback.format_exception(type(error), error, error.__traceback__))
            with open(os.path.join(STATE, "debug.log"), "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {log_token(operation)} {log_token(subject)}\n{redact(detail)}\n")
        except Exception:
            pass


def within_budget(step):
    """False once the time budget is used up, leaving one structured record. Work that follows the decision
    (logging, notices, history, failure counts) is skipped then, so it can never delay the command."""
    if remaining() > 0:
        return True
    log_error("skipped-" + step)
    return False


@contextmanager
def state_lock(name, timeout=2):
    """On a lock timeout or an unusable lock file: log it and carry on without the lock."""
    f, locked = None, False
    end = time.monotonic() + min(timeout, remaining())
    try:
        try:
            f = open(os.path.join(STATE, name + ".lock"), "a", encoding="utf-8")
            while True:
                if platform_compat.try_lock(f):
                    locked = True
                    break
                if time.monotonic() >= end:
                    log_error("lock-timeout", None, name)
                    break
                time.sleep(min(0.01, max(0, end - time.monotonic())))
        except OSError as e:
            log_error("lock-failed", e, name)
        yield locked
    finally:
        if f:
            try:
                if locked:
                    platform_compat.unlock(f)
            except OSError as e:
                log_error("unlock-failed", e, name)
            finally:
                f.close()


def run_local(argv, input=None):
    """Local commands (keychain, ps) share the time budget; a timeout counts as no data."""
    if remaining() <= 0:
        return None
    try:
        return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=min(2, remaining()), input=input)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def recent_failures():
    """Failures in the last 24 hours, counted incrementally by file offset; the first pass scans in blocks and skips non-failure lines unparsed."""
    current = datetime.fromtimestamp(now())
    with state_lock("failures"):
        index = load("failure-index.json", {})
        total = 0
        changed = False
        for name in ("decisions.log", "hook-errors.log"):
            try:
                with open(os.path.join(STATE, name), "rb") as f:
                    stat = os.fstat(f.fileno())
                    saved = index.get(name, {})
                    offset = saved.get("offset", 0)
                    # the file is append-only, so its first bytes identify it: a log that was replaced starts differently.
                    # (st_ino is not used: it means nothing on some Windows file systems.)
                    head = f.read(64).hex()
                    f.seek(max(0, offset - 64))
                    anchor = f.read(min(offset, 64)).hex()
                    # a file that was truncated and grew again is recognised by the bytes before the saved offset.
                    if (not isinstance(saved.get("head"), str) or not head.startswith(saved["head"]) or
                            offset > stat.st_size or saved.get("anchor") != anchor or
                            offset == stat.st_size and saved.get("mtime") != stat.st_mtime_ns):
                        offset, saved = 0, {}
                    counts = {stamp: n for stamp, n in saved.get("counts", {}).items()
                              if 0 <= now() - float(stamp) < 86400}
                    f.seek(offset)
                    pending = b""
                    while remaining() > 0:
                        block = f.read(262144)
                        if not block:
                            break
                        pending += block
                        last = pending.rfind(b"\n")
                        if last < 0:
                            continue
                        complete, pending = pending[:last + 1], pending[last + 1:]
                        offset += len(complete)
                        marker = b" | fail | " if name == "decisions.log" else b" hook"
                        if marker not in complete:
                            continue
                        for raw in complete.splitlines():
                            if marker not in raw:
                                continue
                            line = raw.decode("utf-8", errors="replace")
                            if name == "decisions.log":
                                fields = line.split(" | ", 6)
                                if len(fields) < 6 or fields[5] != "fail":
                                    continue
                            elif " | " in line:
                                # current format: time | operation | exception class | location | subject
                                if line.split(" | ")[1].strip() != "hook":
                                    continue
                            elif not re.search(r"\bhook\s*$", line):
                                continue  # records of earlier versions: "<time> hook" followed by a traceback
                            m = re.match(r"(?:(\d{4})-)?(\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?)", line)
                            if not m:
                                continue
                            try:
                                dt = datetime.fromisoformat(f"{m[1] or current.year}-{m[2]}")
                                if not m[1] and dt > current:
                                    dt = dt.replace(year=current.year - 1)
                                stamp = dt.timestamp()
                            except ValueError:
                                continue  # one bad date must not discard the valid records after it.
                            if 0 <= now() - stamp < 86400:
                                key = str(stamp)
                                counts[key] = counts.get(key, 0) + 1
                    f.seek(max(0, offset - 64))
                    anchor = f.read(min(offset, 64)).hex()
                    entry = dict(head=head, mtime=stat.st_mtime_ns, offset=offset, anchor=anchor, counts=counts)
                    changed |= entry != index.get(name)
                    index[name] = entry
                    total += sum(counts.values())
            except OSError:
                continue
        if changed:
            save("failure-index.json", index)
    return total


def iso_to_ts(s):
    """ISO string → epoch; empty → None (the API returns resets_at = null for an idle five-hour window)."""
    if not s:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def dir_key(d):
    """Directory → account key. None / the default location → the account configured there; not in the pool → None."""
    if d in (None, "", "default"):
        return DEFAULT_KEY["claude"]
    real = platform_compat.canonical(str(d).replace("$HOME", HOME))
    # an account configured with the default directory named explicitly (macOS: its own keychain item) comes first
    for a in CLAUDE_ACCOUNTS:
        if a["dir"] and platform_compat.canonical(a["dir"]) == real:
            return a["key"]
    if real == platform_compat.canonical(config.default_dir("claude")):
        return DEFAULT_KEY["claude"]
    return None


def dir_label(d, english=False):
    """A directory for display: the home directory becomes ~ and separators are forward slashes."""
    if d is None:
        return i18n.EN["label.default_location"] if english else t("label.default_location")
    home = os.path.normcase(HOME).rstrip("\\/")
    if home and os.path.normcase(d).startswith(home) and d[len(home):len(home) + 1] in ("", "/", "\\"):
        d = "~" + d[len(home):]
    return d.replace("\\", "/") if platform_compat.system() == "windows" else d


def detect_lead(hook_data=None):
    """Which account the lead (the session issuing the command) runs on. Returns (key, assumed).
    1. CLAUDE_CONFIG_DIR inherited by this process (the hook and qr are children of the session);
    2. the directory that the hook's transcript_path starts with;
    3. neither → assume the default location, assumed=True."""
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if env_dir:
        k = dir_key(env_dir)
        if k:
            return k, False
    tp = (hook_data or {}).get("transcript_path") or ""
    if tp:
        # the default location and the same directory named explicitly share one transcript directory: the default wins
        for a in sorted(CLAUDE_ACCOUNTS, key=lambda a: a["dir"] is not None):
            d = a["dir"] or config.default_dir("claude")
            if platform_compat.canonical(tp).startswith(platform_compat.canonical(d) + os.sep):
                return a["key"], False
    if os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("CLAUDECODE"):
        return DEFAULT_KEY["claude"], False  # inside a session without CLAUDE_CONFIG_DIR → the default location
    return DEFAULT_KEY["claude"], True


def fmt_left(ts):
    if ts is None:
        return "?"
    s = ts - now()
    if s <= 0:
        return t("time.passed")
    h = s / 3600
    return t("time.in_hours", n=h) if h < 48 else t("time.in_days", n=h / 24)


def fmt_clock(ts):
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "?"


# ---- Claude ----------------------------------------------------------------
def claude_token(acc):
    raw = platform_compat.claude_credentials(acc["dir"], run_local)
    if raw is None:
        return None, None
    try:
        o = json.loads(raw)["claudeAiOauth"]
        return o["accessToken"], o.get("expiresAt", 0) / 1000
    except Exception:
        return None, None


def oauth_get(path, tok):
    req = urllib.request.Request(ENDPOINTS["claude_" + path],
                                 headers={"Authorization": "Bearer " + tok,
                                          "anthropic-beta": "oauth-2025-04-20"})
    return network_json(req)


# ---- renewing an expired Claude login ----------------------------------------
# What Claude Code 2.1.296 does, read from its bundle (~/.local/share/claude/versions/2.1.296, function RSe and its
# callers): when the access token expires within five minutes it POSTs JSON (Content-Type: application/json) to
# TOKEN_URL https://platform.claude.com/v1/oauth/token with
#   {"grant_type": "refresh_token", "refresh_token": ..., "client_id": <clientId stored with the login, else
#    CLIENT_ID 9d1c250a-e61b-44d9-88ed-5944d1962f5e>, "scope": <scopes joined by spaces>}
# with a 30 s timeout. It stores accessToken, refreshToken, expiresAt = now + expires_in (ms),
# refreshTokenExpiresAt (kept when the answer has none) and scopes, keeps subscriptionType and rateLimitTier, and saves
# only when the stored refreshToken is still the one it sent (else it adopts what another process saved), retrying
# the save three times. A 400/401 answer with error "invalid_grant" means the refresh token is dead; the same with
# "account_on_hold" in error_description means the account is on hold. It does not renew a login that has neither
# the user:inference scope nor a subscriptionType. For the default client it asks for its full current scope list
# and, on "invalid_scope", repeats the request with the stored scopes; quota-router sends the stored scopes straight
# away (one request, always a subset of what was granted).
# What the real endpoint did when the supervisor tried it on 2026-10-13:
# - It sits behind Cloudflare, which refuses Python's default User-Agent with HTTP 403 (error 1010) before the
#   request reaches the application; any other honest User-Agent (quota-router/<version>) is let through.
# - A successful answer has token_type "Bearer", access_token, expires_in 28800 (8 h), refresh_token,
#   refresh_token_expires_in 1862778 (about 21.6 days), scope (the same six scopes, space separated), and token_uuid,
#   organization and account (objects; not stored). The refresh token is rotated: the answer carries a new one and
#   the one sent is dead from then on. So once the server has answered, the new refresh token must be saved or that
#   directory is signed out; that is why the renewal runs in a background process of its own (see renewal_main()).
# - A bad refresh token gets HTTP 400 {"error": "invalid_grant", "error_description": "..."}.
# - A handful of failed requests within a minute get HTTP 429 {"error": {"type": "rate_limit_error", ...}}.
REFRESH_MARGIN_SEC = 300       # renew when the access token expires within five minutes, like Claude Code
REFRESH_TIMEOUT_SEC = 30       # the background renewal's socket timeout, as Claude Code's
REFRESH_CEILING_SEC = 120      # ...and the most it waits for a whole answer that trickles in
REFRESH_RETRY_SEC = 600        # after a failed renewal (an answer that was an error, or none), ten minutes until the next
REFRESH_IDLE_SEC = 60          # after "busy" / "none": a minute, so an expired login with a session on it is not re-checked on every dispatch
REFRESH_GAP_SEC = 120          # at most one renewal is started per account in this long
REFRESH_WAIT_SEC = 35          # `qr status --refresh` on a terminal waits this long for the renewals it started
REFRESH_START_SEC = 2          # a reading starts a renewal only with this much of its time budget left (the process list, the start)
REFRESH_LIFE_FALLBACK = 3600   # the expiry stored when an answer's expires_in is unusable
REFRESH_WRITE_PAUSES = (0, 0.2, 0.5)   # one write attempt after each pause, like Claude Code's three
REFRESH_READBACK_PAUSES = (0, 0.2, 0.5)   # a read-back that finds no login at all is repeated after these
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
USER_AGENT = f"quota-router/{__version__}"
ENTRY_FILE = os.path.join(PACKAGE_DIR, "__main__.py")
RENEWALS = []                  # (account key, Popen) of the renewals this process started; `qr status --refresh` waits for them
# the outcomes that only wait REFRESH_IDLE_SEC; every other one waits REFRESH_RETRY_SEC.
REFRESH_IDLE_OUTCOMES = ("busy", "none")
REFRESH_TEXT = {
    "started": "renewal started in the background",
    "renewed": "login renewed",
    "adopted": "already renewed elsewhere",
    "busy": "Claude Code may be using that directory",
    "locked": "Claude Code holds its lock",
    "running": "another renewal is running",
    "none": "nothing to renew with",
    "failed": "renewal failed",
    "odd": "odd answer; new refresh token kept",
    "dead": "refresh token refused; run claude login there",
    "on-hold": "account on hold",
    "unsaved": "renewed but could not be saved",
    "unverified": "renewed and saved; read-back failed",
    "off": "renewal turned off",
}
# the same outcomes of a Codex renewal; only the phrases that name the program differ.
CODEX_REFRESH_TEXT = dict(REFRESH_TEXT, busy="Codex may be using that directory",
                          dead="refresh token refused; run codex login there")


def classify_claude_process(cmd):
    """'claude' for any Claude Code process (a session, a -p job, a subcommand, a resident helper), else None. Any of
    them may renew its directory's login itself, so quota-router leaves that login alone while one runs."""
    argv = split_command_line(cmd)
    if not argv or any(x in argv[0] for x in NOT_SUBAGENT[:3]):
        return None
    exe = program_name(argv[0])
    if exe in SCRIPT_HOSTS:
        script = next((i for i, word in enumerate(argv[1:], 1) if not word.startswith("-")), None)
        exe = script_cli(argv[script]) if script else None
    return "claude" if exe == "claude" else None


def login_busy(acc):
    """True when a Claude Code process may be using this account's directory, and also whenever that cannot be told:
    the process list could not be read, or a Claude Code process's directory is unknown. On Windows another
    process's environment is unreadable, so any Claude Code process whose CLAUDE_CONFIG_DIR is not written on its
    own or an ancestor's command line (a launcher session, the session the hook runs in) makes every account busy."""
    procs = platform_compat.read_processes(run_local)
    if procs is None:
        return True
    default = os.path.join(HOME, ".claude")
    mine = platform_compat.canonical(acc["dir"] or default)
    for pid, (_, cmd) in procs.items():
        if not classify_claude_process(cmd):
            continue
        env = platform_compat.process_environment(pid, cmd, run_local)
        if env is not None:
            found = re.findall(r"(?:^|\s)CLAUDE_CONFIG_DIR=(\S*)", env)
            value = found[-1] if found else None
        else:
            # POSIX: it ended meanwhile, or `ps` gave no answer in time. Windows: only a command line can tell.
            value = platform_compat.directory_from_command_lines(pid, procs, "CLAUDE_CONFIG_DIR")
            if value is None:
                return True
        if platform_compat.canonical(value or default) == mine:
            return True
    return False


def stored_claude_login(acc):
    """(whole stored JSON as a dict, its claudeAiOauth, where it came from), or (None, None, None)."""
    text, where = platform_compat.claude_login(acc["dir"], run_local)
    try:
        data = json.loads(text)
        oauth = data["claudeAiOauth"]
        if isinstance(oauth, dict) and isinstance(oauth.get("accessToken"), str):
            return data, oauth, where
    except (TypeError, ValueError, KeyError):
        pass
    return None, None, None


def login_expiry(oauth):
    value = oauth.get("expiresAt")
    return value / 1000 if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def refresh_disabled(family="claude"):
    return not REFRESH_LOGINS.get(family) or bool(os.environ.get("QR_NO_REFRESH"))


def renewal_blocker(oauth, c):
    """Why this stored login is not to be renewed: "none" (no refresh token, or not a login Claude Code itself would
    renew: no user:inference among its stored scopes), "dead" (the refresh token was refused before or has expired),
    or None when it may be. `c` is the account's cache entry."""
    refresh = oauth.get("refreshToken")
    if not isinstance(refresh, str) or not refresh:
        return "none"
    if c.get("refresh_dead") == fingerprint(refresh):
        return "dead"
    dies = number(oauth.get("refreshTokenExpiresAt"))
    if dies is not None and dies / 1000 <= now():
        c["refresh_dead"] = fingerprint(refresh)
        return "dead"
    scopes = oauth.get("scopes")
    if not (isinstance(scopes, list) and all(isinstance(x, str) for x in scopes) and "user:inference" in scopes):
        return "none"
    if platform_compat.system() == "windows" and os.environ.get("CLAUDE_CODE_FORCE_WINDOWS_CREDMAN"):
        return "none"           # the live login is in the Credential Manager; a .credentials.json is a leftover
    return None


def renewal_error(state):
    """The error a reading shows for an expired login, given where its renewal stands."""
    return t({"dead": "err.login_dead", "on-hold": "err.login_on_hold",
              "unsaved": "err.refresh_unsaved"}.get(state, "err.expired"))


def start_renewal(acc, c, family="claude"):
    """Called by a reading (the hook, qr status / pick / check / launch) that finds a login about to expire: a Claude
    one within five minutes, a Codex one within refresh_before_hours. It never contacts the token endpoint itself:
    when the login may be renewed it starts one background process (`renew-login <key>`, see renewal_main()) and
    returns "started". Otherwise it returns why not: "off", "budget", the outcome of the last attempt while its
    cooldown runs, "adopted" (no longer due), "busy", "none", "dead" or "running" (one was started less than
    REFRESH_GAP_SEC ago). `c` is the account's cache entry; the cooldown lives there."""
    if refresh_disabled(family):
        return "off"
    if c.get("refresh_retry_at", 0) > now():
        return c.get("refresh_failed", "failed")
    for field in ("refresh_failed", "refresh_retry_at"):     # an earlier attempt's outcome no longer applies
        c.pop(field, None)
    if remaining() < REFRESH_START_SEC:
        return "budget"
    known_dead = c.get("refresh_dead")
    state = renewal_state(acc, c, family)
    if state == "adopted":
        return state
    if state:
        c["refresh_failed"] = state
        c["refresh_retry_at"] = now() + (REFRESH_IDLE_SEC if state in REFRESH_IDLE_OUTCOMES else REFRESH_RETRY_SEC)
        # a refresh token found dead here for the first time (its stored expiry has passed) is not sent anywhere, but
        # it still gets one background run, which records the outcome and runs the [notify] command.
        if state != "dead" or c.get("refresh_dead") == known_dead:
            return state
    marker = os.path.join(STATE, f"refresh-{acc['key']}.start")
    try:
        # removing an old marker and claiming a new one with O_EXCL both happen under the cache lock, like the Codex probe's.
        with state_lock("cache"):
            try:
                if now() - os.stat(marker).st_mtime >= REFRESH_GAP_SEC:
                    os.unlink(marker)
            except FileNotFoundError:
                pass
            try:
                os.close(os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            except FileExistsError:
                return state or "running"
        process = spawn_renewal(acc)
    except Exception as e:
        log_error("login-refresh-start", e, acc["key"])
        return state or "failed"
    if process is not None:
        RENEWALS.append((acc["key"], process))
    log_decision("refresh", family, "refresh-started", account=acc["key"], text=REFRESH_TEXT["started"])
    return state or "started"


def renewal_state(acc, c, family):
    """Whether a stored login is to be renewed now: None when it is, "adopted" when it is not due (any more), or why
    not ("none", "dead", "busy"). `c` is the account's cache entry."""
    if family == "codex":
        data, tokens = stored_codex_login(acc)
        expiry = codex_expiry(data, tokens) if tokens else None
        if expiry is None:
            return "none"
        if expiry - now() >= CODEX_REFRESH_BEFORE_SEC:
            return "adopted"
        return codex_renewal_blocker(data, tokens, c) or ("busy" if codex_login_busy(acc, expiry) else None)
    _, oauth, where = stored_claude_login(acc)
    if oauth is None or where is None:
        return "none"
    if login_expiry(oauth) - now() >= REFRESH_MARGIN_SEC:
        return "adopted"
    return renewal_blocker(oauth, c) or ("busy" if login_busy(acc) else None)


def spawn_renewal(acc):
    """Start `python -I -X utf8 <this package>/__main__.py renew-login <key>` detached, its output appended to
    refresh-<key>.log in the state directory. Separate so tests can replace it."""
    with open(os.path.join(STATE, f"refresh-{acc['key']}.log"), "a", encoding="utf-8") as log:
        return platform_compat.spawn_detached([sys.executable, "-I", "-X", "utf8", ENTRY_FILE, "renew-login", acc["key"]],
                                              env=dict(os.environ), stdout=log, stderr=log)


def wait_for_renewals(seconds=REFRESH_WAIT_SEC):
    """Wait, up to `seconds` in all, for the renewals this process started. True when at least one has ended."""
    end, ended = time.monotonic() + seconds, False
    while RENEWALS:
        _, process = RENEWALS.pop()
        try:
            process.wait(timeout=max(0.0, end - time.monotonic()))
            ended = True
        except Exception:
            pass
    return ended


def renewal_main(argv):
    """`qr renew-login <key>`: the background renewal a reading started (see start_renewal()). Not a user command.

    It runs outside every hook's time budget, takes the account's quota-router lock and Claude Code's three locks
    (claude_locks()), checks again that no Claude Code process uses the directory and that the login still needs
    renewing, POSTs with a 30 s timeout, and writes the answer back with up to three attempts. From the POST until
    the write is confirmed it ignores SIGINT, SIGTERM and SIGHUP (on Windows SIGINT and SIGBREAK only; a
    TerminateProcess, as Task Manager or `taskkill /F` do, cannot be refused). Every outcome is one line in
    decisions.log and one in refresh-<key>.log; a failure also one in hook-errors.log, with the HTTP status code when
    there was one. No token, no answer body and no error_description is written anywhere but the credential store."""
    family = next((f for f in ("claude", "codex") for a in accounts(f) if argv and a["key"] == argv[0]), None)
    acc = next((a for a in accounts(family) if a["key"] == argv[0]), None) if family else None
    if acc is None:
        return 2
    try:
        outcome = renew_codex_login(acc) if family == "codex" else renew_login(acc)
    except BaseException as e:            # anything at all: one structured line, never a traceback
        outcome = finish_renewal(acc, "failed", error=e, family=family)
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {outcome}", flush=True)
    return 0


def renew_login(acc):
    if refresh_disabled():
        return "off"
    with deferred_alerts(), state_lock("refresh-" + acc["key"], timeout=0) as locked:
        if not locked:
            return finish_renewal(acc, "busy", text=REFRESH_TEXT["running"])
        held = platform_compat.take_claude_refresh_lock(acc["dir"])
        if held is None:
            return finish_renewal(acc, "busy", text=REFRESH_TEXT["locked"])
        try:
            with platform_compat.LockHeartbeat(held):
                return renew_locked(acc)
        finally:
            platform_compat.release_claude_refresh_lock(held)


def renew_locked(acc):
    """The part of renew_login() that runs with every lock held."""
    data, oauth, where = stored_claude_login(acc)
    if oauth is None or where is None:
        return finish_renewal(acc, "none")
    if login_expiry(oauth) - now() >= REFRESH_MARGIN_SEC:
        return finish_renewal(acc, "adopted")            # renewed by someone else meanwhile
    c = load("cache.json", {}).get(acc["key"], {})
    blocker = renewal_blocker(oauth, c)
    if blocker:
        return finish_renewal(acc, blocker, dead=c.get("refresh_dead"))
    if where[0] == "keychain" and where[2] is None:
        account = platform_compat.keychain_account(where[1], run_local)
        if not account:
            return finish_renewal(acc, "none")
        where = (where[0], where[1], account)
    if where[0] == "file" and not os.path.isfile(where[1]):
        return finish_renewal(acc, "none")
    if login_busy(acc):
        return finish_renewal(acc, "busy")
    refresh = oauth["refreshToken"]
    client = oauth.get("clientId") if isinstance(oauth.get("clientId"), str) and oauth.get("clientId") else CLAUDE_CLIENT_ID
    body = json.dumps({"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client,
                       "scope": " ".join(oauth["scopes"])}).encode()
    with interruption_shield():
        try:
            answer = post_token(body)
        except urllib.error.HTTPError as e:
            code, description = oauth_error(e)
            if e.code in (400, 401, 403) and code in ("invalid_grant", "access_denied") and "account_on_hold" in description:
                return finish_renewal(acc, "on-hold", error=e)
            if e.code in (400, 401) and code == "invalid_grant":
                return finish_renewal(acc, "dead", error=e, dead=fingerprint(refresh))
            return finish_renewal(acc, "failed", error=e)          # 429 and 5xx included: never retried here
        except Exception as e:
            return finish_renewal(acc, "failed", error=e)
        try:
            fields, complete = renewed_login(oauth, answer, refresh)
        except ValueError as e:
            return finish_renewal(acc, "failed", error=e)
        written = write_renewed_login(acc, where, refresh, fields)
    if written != "renewed":
        return finish_renewal(acc, written, error=OSError(written))
    if not complete:
        return finish_renewal(acc, "failed", text=REFRESH_TEXT["odd"])
    return finish_renewal(acc, "renewed", old=oauth["accessToken"], new=fields["accessToken"])


@contextmanager
def interruption_shield():
    """Ignore the signals that ask a process to stop while the block runs; restore them afterwards."""
    saved = {}
    import signal, threading
    if threading.current_thread() is threading.main_thread():
        for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):
            number_ = getattr(signal, name, None)
            if number_ is None:
                continue
            try:
                saved[number_] = signal.signal(number_, signal.SIG_IGN)
            except (OSError, ValueError, RuntimeError):
                pass
    try:
        yield
    finally:
        for number_, handler in saved.items():
            try:
                signal.signal(number_, handler)
            except (OSError, ValueError, RuntimeError, TypeError):
                pass


def post_token(body, endpoint="claude_token"):
    """The one POST to the token endpoint; redirects are never followed (NoRedirect)."""
    req = urllib.request.Request(ENDPOINTS[endpoint], data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json",
                                          "User-Agent": USER_AGENT})

    def fetch():
        with open_url(req, timeout=REFRESH_TIMEOUT_SEC) as response:
            return json.load(response)
    return platform_compat.call_with_deadline(fetch, REFRESH_CEILING_SEC, lambda: TimeoutError("token answer too slow"))


def oauth_error(error):
    """(the `error` code of an OAuth error answer or None, its error_description or ""). `error` may also be an
    object with a `type` (the 429 answer). Neither is ever logged."""
    try:
        body = json.loads(error.read(65536) or b"{}")
        code = body.get("error")
        code = code.get("type") if isinstance(code, dict) else code
        description = body.get("error_description")
        return (code if isinstance(code, str) else None), (description if isinstance(description, str) else "")
    except Exception:
        return None, ""


def renewed_login(oauth, answer, refresh):
    """(the claudeAiOauth fields a token answer changes, whether it is a complete renewal).

    A complete one has an access token, a usable expires_in (else REFRESH_LIFE_FALLBACK is assumed) and, when it
    names scopes, user:inference among them. An answer that is not complete but carries a new refresh token still
    yields that token: the one sent is dead by now, so dropping it would sign the directory out. Raises ValueError
    when the answer holds nothing worth storing."""
    if not isinstance(answer, dict):
        raise ValueError("token answer is not an object")
    at = now()
    fields = {}
    rotated = answer.get("refresh_token")
    if isinstance(rotated, str) and rotated and rotated != refresh:
        fields["refreshToken"] = rotated
        longevity = number(answer.get("refresh_token_expires_in"))
        if longevity is not None and 0 < longevity < 10 ** 10:
            fields["refreshTokenExpiresAt"] = int((at + longevity) * 1000)
    access = answer.get("access_token")
    scope = answer.get("scope")
    scopes = scope.split() if isinstance(scope, str) and scope.split() else None
    # Claude Code does not keep a login without inference access either; it would no longer be a Claude.ai login.
    if isinstance(access, str) and access and "user:inference" in (scopes or oauth["scopes"]):
        life = number(answer.get("expires_in"))
        life = life if life is not None and 0 < life < 10 ** 9 else REFRESH_LIFE_FALLBACK
        fields.update(accessToken=access, expiresAt=int((at + life) * 1000))
        if scopes:
            fields["scopes"] = scopes
        if "refreshToken" not in fields:
            longevity = number(answer.get("refresh_token_expires_in"))
            if longevity is not None and 0 < longevity < 10 ** 10:
                fields["refreshTokenExpiresAt"] = int((at + longevity) * 1000)
        return fields, True
    if "refreshToken" in fields:
        return fields, False
    raise ValueError("token answer without a usable access token")


def write_renewed_login(acc, where, refresh, fields):
    """Save `fields` over the stored login, compare-and-set on the refresh token that was sent, as Claude Code does.
    Up to three attempts, each re-reading the store first; a read-back confirms the write. Returns "renewed",
    "unverified" (written, but the read-back found no login), "unsaved" or "conflict" (the stored login changed
    to one this renewal did not write, which a process that ignores Claude Code's locks could do)."""
    outcome = "unsaved"
    for pause in REFRESH_WRITE_PAUSES:
        time.sleep(pause)
        latest, current, _ = stored_claude_login(acc)
        if current is None:
            continue                                         # the store did not answer this time
        if all(current.get(k) == v for k, v in fields.items()):
            return "renewed"                                 # an earlier attempt landed after all
        if current.get("refreshToken") != refresh:
            return "conflict"
        # everything else in the stored JSON (mcpOAuth, fields this code does not know) is kept as it is now.
        text = json.dumps(dict(latest, claudeAiOauth=dict(current, **fields)), ensure_ascii=False, separators=(",", ":"))
        try:
            platform_compat.save_claude_login(where, text, run_local)
        except Exception as e:
            log_error("login-refresh-save", e, acc["key"])
            continue
        for wait in REFRESH_READBACK_PAUSES:
            time.sleep(wait)
            back = stored_claude_login(acc)[1]
            if back is not None:
                break
        if back is None:
            outcome = "unverified"
            break
        if all(back.get(k) == v for k, v in fields.items()):
            return "renewed"
    return outcome


def finish_renewal(acc, outcome, error=None, text=None, dead=None, old=None, new=None, family="claude"):
    """Record how a background renewal ended: the account's cooldown in cache.json, one line in decisions.log, and
    for a failure one in hook-errors.log and a run of the [notify] command. Returns the outcome."""
    shown = {"conflict": "unsaved"}.get(outcome, outcome)
    with state_lock("cache"):
        latest = load("cache.json", {})
        c = latest.setdefault(acc["key"], {})
        if shown in ("renewed", "adopted", "unverified"):
            for field in ("refresh_failed", "refresh_retry_at", "refresh_dead"):
                c.pop(field, None)
            if old and new and c.get("token_sig") == fingerprint(old):
                c["token_sig"] = fingerprint(new)            # the same login with a new token: its readings stay
        elif shown != "off":
            c["refresh_failed"] = shown
            c["refresh_retry_at"] = now() + (REFRESH_IDLE_SEC if shown in REFRESH_IDLE_OUTCOMES else REFRESH_RETRY_SEC)
            if shown == "dead" and dead:
                c["refresh_dead"] = dead
        save("cache.json", latest)
    if error is not None:
        log_error("login-refresh-save" if shown in ("unsaved", "unverified") else "login-refresh", error, acc["key"])
    if text is None:
        text = (CODEX_REFRESH_TEXT if family == "codex" else REFRESH_TEXT)[shown]
        status = getattr(error, "code", None) if isinstance(error, urllib.error.HTTPError) else None
        if shown == "failed" and isinstance(status, int):
            text += f", HTTP {status}"
    mode = "refresh" if shown == "renewed" else "refresh-" + shown
    log_decision("refresh", family, mode, account=acc["key"], text=text)
    if shown in NOTIFY_OUTCOMES:
        pending = ALERTS.get()
        if pending is not None:
            pending.append((family, acc, shown, text))      # run once the locks and the signal shield are left
        else:
            notify_refresh_failed(family, acc, shown, text)
    elif shown in ("renewed", "adopted"):
        forget_alerts(family, acc["key"])
    return outcome


# ---- telling someone that a renewal failed -------------------------------------
# [notify] on_refresh_failed is a shell command. It runs in the background renewal, after the outcome has been
# recorded, the account's locks released and the signal shield left, with QR_FAMILY, QR_KEY, QR_REASON (one of
# NOTIFY_OUTCOMES), QR_DETAIL (the decisions.log phrase), QR_DIR and, when known, QR_EMAIL and QR_EXPIRES_AT (the
# access token's expiry, ISO 8601 local time); of quota-router's own environment it gets only NOTIFY_ENV. The same
# account and reason runs it at most once per repeat_hours (an hour, when the command itself failed); "failed" (a
# temporary failure) only after failed_attempts of them in a row. A renewal that succeeds clears both, so the next
# failure is told as if it were the first. Its output goes to notify.log; it never changes the renewal.
NOTIFY_OUTCOMES = ("failed", "dead", "on-hold", "unsaved")
NOTIFY_TIMEOUT_SEC = 30
NOTIFY_RETRY_SEC = 3600
NOTIFY_TEXT = {"sent": "alert command ran", "failed": "alert command failed", "skipped": "alert already sent",
               "waiting": "alert waits for another failure", "locked": "alert skipped, state lock busy"}
# the variables of quota-router's environment the command gets (and LC_*); the second half is what cmd.exe needs.
NOTIFY_ENV = ("PATH", "HOME", "LANG", "TMPDIR", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE")
ALERTS = ContextVar("quota_alerts", default=None)


@contextmanager
def deferred_alerts():
    """Alerts of the renewal inside the block run when the block is left: after the account's locks are released and
    outside interruption_shield(), so a slow command holds up neither."""
    token = ALERTS.set([])
    try:
        yield
    finally:
        pending = ALERTS.get() or []
        ALERTS.reset(token)
        for args in pending:
            notify_refresh_failed(*args)


def notify_refresh_failed(family, acc, reason, detail):
    """Run the [notify] command for one failed renewal, unless it already ran for this account and reason recently.
    Returns "sent", "failed", "skipped", "waiting" (a "failed" not yet repeated failed_attempts times) or None (no
    command configured). Without the state lock nothing runs (rather one alert too few than one too many). Never
    raises."""
    if not NOTIFY_COMMAND:
        return None
    key = acc["key"]
    slot = f"{family}:{key}:{reason}"
    try:
        with state_lock("notify", timeout=5) as locked:
            if not locked:
                log_decision("refresh", family, "notify-skipped", account=key, text=NOTIFY_TEXT["locked"])
                return "skipped"
            sent = load("notify-state.json", {})
            sent = sent if isinstance(sent, dict) else {}
            if reason == "failed":
                # consecutive: a failure more than repeat_hours after the previous one starts the count again.
                streak = f"{family}:{key}:failed-streak"
                previous = sent.get(streak) if isinstance(sent.get(streak), dict) else {}
                count = int(number(previous.get("count")) or 0)
                if now() - (number(previous.get("at")) or 0) > NOTIFY_REPEAT_SEC:
                    count = 0
                sent[streak] = {"count": count + 1, "at": now()}
                if count + 1 < NOTIFY_FAILED_ATTEMPTS:
                    save("notify-state.json", sent)
                    log_decision("refresh", family, "notify-skipped", account=key, text=NOTIFY_TEXT["waiting"])
                    return "waiting"
            last = sent.get(slot) if isinstance(sent.get(slot), dict) else {}
            at, ok = number(last.get("at")) or 0, last.get("ok")
            if ok is None and last and now() - at > NOTIFY_TIMEOUT_SEC + 30:
                ok = False                                     # claimed, but the run never reported back (killed)
            wait = NOTIFY_RETRY_SEC if ok is False else NOTIFY_REPEAT_SEC
            if now() - at < wait:
                save("notify-state.json", sent)
                log_decision("refresh", family, "notify-skipped", account=key, text=NOTIFY_TEXT["skipped"])
                return "skipped"
            stamp = now()
            sent[slot] = {"at": stamp, "ok": None}             # claimed before it runs: a second failure meanwhile waits
            save("notify-state.json", sent)
        ok = run_notify(family, acc, reason, detail)
        with state_lock("notify", timeout=5) as locked:
            sent = load("notify-state.json", {}) if locked else None
            if isinstance(sent, dict) and isinstance(sent.get(slot), dict) and sent[slot].get("at") == stamp:
                sent[slot]["ok"] = ok
                save("notify-state.json", sent)
        outcome = "sent" if ok else "failed"
        log_decision("refresh", family, "notify" if ok else "notify-failed", account=key, text=NOTIFY_TEXT[outcome])
        return outcome
    except Exception as e:
        log_error("notify", e, key)
        return "failed"


def forget_alerts(family, key):
    """A renewal succeeded: the next failure of this account is told as if it were the first."""
    try:
        with state_lock("notify", timeout=5) as locked:
            sent = load("notify-state.json", {}) if locked else None
            if not isinstance(sent, dict):
                return
            stale = [slot for slot in sent if slot.startswith(f"{family}:{key}:")]
            if stale:
                for slot in stale:
                    sent.pop(slot)
                save("notify-state.json", sent)
    except Exception as e:
        log_error("notify", e, key)


def login_facts(family, acc):
    """(directory, email or None, access token expiry or None) of an account, for the [notify] command."""
    if family == "codex":
        data, tokens = stored_codex_login(acc)
        if not tokens:
            return acc["dir"], None, None
        return acc["dir"], codex_email(tokens), codex_expiry(data, tokens)
    email = (load("cache.json", {}).get(acc["key"]) or {}).get("email")
    expiry = quiet_call(lambda: login_expiry(stored_claude_login(acc)[1] or {}))
    return acc["dir"] or os.path.join(HOME, ".claude"), email if isinstance(email, str) else None, expiry or None


def run_notify(family, acc, reason, detail):
    """Run NOTIFY_COMMAND once through the shell. True when it exited with status 0 within NOTIFY_TIMEOUT_SEC."""
    directory, email, expiry = quiet_call(login_facts, family, acc, default=(acc["dir"], None, None))
    env = {name: value for name, value in os.environ.items() if name in NOTIFY_ENV or name.startswith("LC_")}
    env.update(QR_FAMILY=family, QR_KEY=acc["key"], QR_REASON=reason, QR_DETAIL=detail, QR_DIR=directory or "")
    if email:
        env["QR_EMAIL"] = email
    if expiry:
        env["QR_EXPIRES_AT"] = datetime.fromtimestamp(expiry).astimezone().isoformat(timespec="seconds")
    with open(os.path.join(STATE, "notify.log"), "a", encoding="utf-8") as log:
        log.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {log_token(family)} {log_token(acc['key'])} {log_token(reason)}\n")
        log.flush()
        try:
            code = run_notify_command(NOTIFY_COMMAND, env, log)
        except subprocess.TimeoutExpired:
            code = "timeout"
        except OSError as e:
            code = type(e).__name__
        log.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} exit {code}\n")
    return code == 0


def run_notify_command(command, env, log):
    """Separate so tests can replace it. The command's output is appended to notify.log. On macOS and Linux it runs in
    a session of its own, and a timeout ends that whole process group (the shell and whatever it started, a curl
    say), not only the shell."""
    import signal
    posix = platform_compat.system() != "windows"
    process = subprocess.Popen(command, shell=True, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                               start_new_session=posix)
    try:
        return process.wait(timeout=NOTIFY_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        try:
            if posix:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise


# ---- renewing a Codex login ------------------------------------------------------
# What Codex 0.160.1 does (openai/codex, codex-rs/login/src/auth/manager.rs and oauth/client.rs, tag rust-v0.160.1):
# it renews when the access token (a JWT; `exp`) expires within five minutes, or, when that cannot be read, when
# last_refresh is over eight days old; and after a 401 from the backend, which first re-reads auth.json and uses
# what another process saved there. The request is one JSON POST to https://auth.openai.com/oauth/token (override:
# CODEX_REFRESH_TOKEN_URL_OVERRIDE) with {"client_id": "app_EMoamEEZ73f0CkXaXp7hrann", "grant_type": "refresh_token",
# "refresh_token": ...}. The answer may hold id_token, access_token and refresh_token; Codex stores those present
# into tokens, sets last_refresh to now and rewrites auth.json in place (mode 600). Any 401, a 400 with
# "invalid_grant", or the codes refresh_token_expired / _reused / _invalidated ("error", "error.code" or "code") are
# permanent: "Please log out and sign in again". Codex takes no lock on auth.json other processes could see.
# quota-router assumes the refresh token is rotated (the answer carries a new one and the old one is dead from then
# on), so the answer is written back with the same care as a Claude login: three compare-and-set attempts on the
# refresh token that was sent, each followed by a read-back, and no stop signal between the POST and that write.
# Unlike Claude's eight-hour token, Codex's access token lasts about ten days and the readings that start a
# renewal may come once a day (and not on Sundays here), so it is renewed refresh_before_hours (default 72) before it
# expires.
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_FALLBACK_LIFE_SEC = 8 * 86400    # no readable `exp`: Codex renews eight days after last_refresh
CODEX_HOT_SEC = 1800                   # from this long before the expiry on (and after it) any Codex process may renew it
# the codes Codex treats as a dead refresh token whatever the status code; "invalid_grant" only with a 400.
CODEX_DEAD_CODES = ("refresh_token_expired", "refresh_token_reused", "refresh_token_invalidated")
# Codex processes that stay up for hours: the app-server of the ChatGPT / Codex desktop app (also as a daemon),
# exec-server (mcp-server is not a subcommand of 0.160.1 / 0.163.0; kept for older and later versions). They are told
# apart from jobs only for the process classifier's tests and the docs; both count the same way (codex_login_busy).
CODEX_RESIDENT = ("app-server", "exec-server", "mcp-server", "code-mode-host")
CODEX_JOB_WORDS = ("exec", "e", "resume")
CODEX_LOGIN_WORDS = ("login", "logout")  # these write auth.json themselves, at any time
# after a refusal, auth.json is read again after each of these pauses before the refresh token is called dead: Codex
# may have renewed it meanwhile (its POST answered, its write still to come), and its in-place write can be caught
# half done.
CODEX_DEAD_RECHECK_PAUSES = (0.5, 2, 5)
# Codex's top-level options that take a value (`-c key=value`, `--profile p`), skipped with it to find the subcommand.
CODEX_VALUE_OPTIONS = ("-c", "--config", "-p", "--profile", "-C", "--cd", "-m", "--model", "--enable", "--disable",
                       "-a", "--ask-for-approval", "-s", "--sandbox", "-i", "--image", "--add-dir", "--local-provider",
                       "--remote", "--remote-auth-token-env")


def jwt_claims(token):
    """The payload of a JWT as a dict, or None. The signature is not checked; only `exp` and `email` are read."""
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return claims if isinstance(claims, dict) else None
    except Exception:
        return None


def codex_auth_file(acc):
    return os.path.join(acc["dir"], "auth.json")


def stored_codex_login(acc):
    """(whole auth.json as a dict, its tokens) when it holds a ChatGPT login with an access token, else (None, None)."""
    try:
        with open(codex_auth_file(acc), encoding="utf-8") as f:
            data = json.load(f)
        tokens = data.get("tokens")
        if isinstance(tokens, dict) and isinstance(tokens.get("access_token"), str) and tokens["access_token"]:
            return data, tokens
    except (OSError, ValueError, AttributeError):
        pass
    return None, None


def codex_expiry(data, tokens):
    """When the stored access token expires (epoch), as Codex judges it: its `exp`, else last_refresh + eight days."""
    exp = number((jwt_claims(tokens.get("access_token") or "") or {}).get("exp"))
    if exp:
        return exp
    try:
        last = iso_to_ts((data or {}).get("last_refresh"))
    except (TypeError, ValueError, AttributeError):
        last = None
    return last + CODEX_FALLBACK_LIFE_SEC if last else None


def codex_email(tokens):
    claims = jwt_claims(tokens.get("id_token") or "") or {}
    email = claims.get("email") or (claims.get("https://api.openai.com/profile") or {}).get("email")
    return email if isinstance(email, str) and email else None


def codex_renewal_blocker(data, tokens, c):
    """Why this stored Codex login is not to be renewed: "none" (no refresh token, or not Codex's own ChatGPT login:
    an API key, or tokens handed to Codex by a host), "dead" (that refresh token was refused before), or None."""
    refresh = tokens.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        return "none"
    if data.get("auth_mode") not in (None, "chatgpt"):
        return "none"
    if c.get("refresh_dead") == fingerprint(refresh):
        return "dead"
    return None


def classify_codex_process(cmd):
    """'login' for codex login / logout, 'resident' for a long-running Codex server (CODEX_RESIDENT), 'job' for any
    other Codex process (codex exec, an interactive session, a subcommand), None for anything else."""
    argv = split_command_line(cmd)
    if not argv:
        return None
    exe = program_name(argv[0])
    if exe in SCRIPT_HOSTS:
        script = next((i for i, word in enumerate(argv[1:], 1) if not word.startswith("-")), None)
        exe = script_cli(argv[script]) if script else None
        argv = argv[script:] if script else argv
    if exe != "codex":
        return None
    # The subcommand comes after the top-level options. Options known to take a value are skipped with it; any other
    # word that is not a known subcommand (the value of an option this list lacks, a prompt) is passed over, so that
    # `codex --new-option x login` is still a login. Nothing after exec / resume is read: that is the prompt.
    words = iter(argv[1:])
    for word in words:
        if word in CODEX_VALUE_OPTIONS:
            next(words, None)
        elif word in CODEX_LOGIN_WORDS:
            return "login"
        elif word in CODEX_JOB_WORDS:
            return "job"
        elif word in CODEX_RESIDENT:
            return "resident"
    return "job"


def codex_login_busy(acc, expiry):
    """True when a Codex process may renew this account's login meanwhile, and whenever that cannot be told.

    `codex login` / `codex logout` on that CODEX_HOME always count. Any other Codex process (a job, a session, the
    desktop app's app-server that runs all day on ~/.codex) counts from CODEX_HOT_SEC before the expiry on, and
    after it: Codex renews on its own when the token expires within five minutes (so on every use once it has
    expired), and otherwise only after a 401 from its backend or when the desktop app asks it to (getAuthStatus or
    account/read with a refresh flag), all of which
    re-read auth.json first and take over what was saved here. Fails closed: an unreadable process list, or a
    counting Codex process whose CODEX_HOME cannot be told, counts as busy."""
    procs = platform_compat.read_processes(run_local)
    if procs is None:
        return True
    hot = expiry - now() < CODEX_HOT_SEC
    mine = platform_compat.canonical(acc["dir"])
    default = platform_compat.canonical(config.default_dir("codex"))
    for pid, (_, cmd) in procs.items():
        kind = classify_codex_process(cmd)
        if not kind or (kind != "login" and not hot):
            continue
        env = platform_compat.process_environment(pid, cmd, run_local)
        if env is not None:
            found = re.findall(r"(?:^|\s)CODEX_HOME=(\S*)", env)
            value = found[-1] if found else None
        else:
            value = platform_compat.directory_from_command_lines(pid, procs, "CODEX_HOME")
            if value is None:
                return True
        if (platform_compat.canonical(value) if value else default) == mine:
            return True
    return False


def renew_codex_login(acc):
    if refresh_disabled("codex"):
        return "off"
    with deferred_alerts(), state_lock("refresh-" + acc["key"], timeout=0) as locked:
        if not locked:
            return finish_renewal(acc, "busy", text=CODEX_REFRESH_TEXT["running"], family="codex")
        return renew_codex_locked(acc)


def renew_codex_locked(acc):
    """The part of renew_codex_login() that runs with the account's lock held."""
    finish = lambda outcome, **kw: finish_renewal(acc, outcome, family="codex", **kw)
    data, tokens = stored_codex_login(acc)
    expiry = codex_expiry(data, tokens) if tokens else None
    if expiry is None:
        return finish("none")
    if expiry - now() >= CODEX_REFRESH_BEFORE_SEC:
        return finish("adopted")                                # renewed by Codex or someone else meanwhile
    c = load("cache.json", {}).get(acc["key"], {})
    blocker = codex_renewal_blocker(data, tokens, c)
    if blocker:
        return finish(blocker, dead=c.get("refresh_dead"))
    if codex_login_busy(acc, expiry):
        return finish("busy")
    refresh = tokens["refresh_token"]
    body = json.dumps({"client_id": CODEX_CLIENT_ID, "grant_type": "refresh_token", "refresh_token": refresh}).encode()
    with interruption_shield():
        try:
            answer = post_token(body, "codex_token")
        except urllib.error.HTTPError as e:
            code = codex_error_code(e)
            if e.code == 401 or code in CODEX_DEAD_CODES or (e.code == 400 and code == "invalid_grant"):
                current = None
                for pause in CODEX_DEAD_RECHECK_PAUSES:
                    time.sleep(pause)
                    current = stored_codex_login(acc)[1]
                    if current is not None and current.get("refresh_token") != refresh:
                        # Codex (or a new login) replaced it meanwhile; the refusal was for the old one.
                        return finish("adopted")
                if current is None:
                    # the last read found no login: it may be mid-write; not called dead on that, tried again later.
                    return finish("failed", error=e)
                return finish("dead", error=e, dead=fingerprint(refresh))
            return finish("failed", error=e)                   # 429 and 5xx included: never retried here
        except Exception as e:
            return finish("failed", error=e)
        try:
            fields, complete = renewed_codex_tokens(answer, refresh)
        except ValueError as e:
            return finish("failed", error=e)
        written = write_codex_login(acc, refresh, fields)
    if written != "renewed":
        return finish(written, error=OSError(written))
    if not complete:
        return finish("failed", text=CODEX_REFRESH_TEXT["odd"])
    return finish("renewed", old=tokens["access_token"], new=fields["access_token"])


def codex_error_code(error):
    """The error code of a refused Codex token request, lower case, read the way Codex reads it, or None. Never logged."""
    try:
        body = json.loads(error.read(65536) or b"{}")
        value = body.get("error")
        code = value.get("code") if isinstance(value, dict) else value
        if not (isinstance(code, str) and code.strip()):
            code = body.get("code")
        return code.strip().lower() if isinstance(code, str) and code.strip() else None
    except Exception:
        return None


def renewed_codex_tokens(answer, refresh):
    """(the `tokens` fields a token answer changes, whether it is a complete renewal). A complete one has an access
    token. An id_token is taken only when it is a readable JWT (Codex refuses to load an auth.json whose id_token is
    not), else the stored one stays. An answer without an access token but with a new refresh token still yields that
    token, since the one sent is dead by now. Raises ValueError when the answer holds nothing worth storing."""
    if not isinstance(answer, dict):
        raise ValueError("token answer is not an object")
    fields = {}
    rotated = answer.get("refresh_token")
    if isinstance(rotated, str) and rotated and rotated != refresh:
        fields["refresh_token"] = rotated
    access = answer.get("access_token")
    if isinstance(access, str) and access:
        fields["access_token"] = access
        identity = answer.get("id_token")
        if isinstance(identity, str) and jwt_claims(identity) is not None:
            fields["id_token"] = identity
        return fields, True
    if fields:
        return fields, False
    raise ValueError("token answer without a usable access token")


def write_codex_login(acc, refresh, fields):
    """Save `fields` into auth.json's tokens (and last_refresh = now), compare-and-set on the refresh token that was
    sent. Up to three attempts, each re-reading the file first; a read-back confirms the write. Everything else in the
    file is kept as it is. Returns "renewed", "unverified", "unsaved" or "conflict" (the file now holds a login this
    renewal did not write: Codex renewed it meanwhile, or someone logged in again)."""
    outcome = "unsaved"
    for pause in REFRESH_WRITE_PAUSES:
        time.sleep(pause)
        data, current = stored_codex_login(acc)
        if current is None:
            continue                                         # unreadable this time (or mid-write by Codex)
        if all(current.get(k) == v for k, v in fields.items()):
            return "renewed"                                 # an earlier attempt landed after all
        if current.get("refresh_token") != refresh:
            return "conflict"
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        try:
            save_codex_auth(codex_auth_file(acc), dict(data, tokens=dict(current, **fields), last_refresh=stamp))
        except Exception as e:
            log_error("login-refresh-save", e, acc["key"])
            continue
        back = None
        for wait in REFRESH_READBACK_PAUSES:
            time.sleep(wait)
            back = stored_codex_login(acc)[1]
            if back is not None:
                break
        if back is None:
            outcome = "unverified"
            break
        if all(back.get(k) == v for k, v in fields.items()):
            return "renewed"
    return outcome


def save_codex_auth(path, data):
    """Replace auth.json as a whole: a temporary file next to it (mode 600, written and flushed to disk), renamed over
    it. A symbolic link is followed, so the file it points to is the one replaced. It is never created where there
    was none."""
    target = os.path.realpath(path)
    if not os.path.isfile(target):
        raise FileNotFoundError(target)
    tmp = os.path.join(os.path.dirname(target), f".auth.json.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
            f.flush()
            os.fsync(f.fileno())
        platform_compat.replace(tmp, target)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


class BudgetTimeout(TimeoutError):
    """This dispatch ran out of time; it does not mean the usage service is down."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A login token goes to the configured endpoint and nowhere else: no redirect is ever followed, whatever
    its status code or target. The 3xx answer then surfaces as an HTTPError, which is a failed query."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def open_url(req, timeout):
    """Separate so tests can replace it."""
    return OPENER.open(req, timeout=timeout)


def network_json(req):
    """One GET, parsed. The whole exchange, including a response body that trickles in, is bounded by what is
    left of the time budget; each socket operation is additionally capped at four seconds, so a request that
    is abandoned at the deadline ends by itself shortly after."""
    left = remaining()
    if left < 1:
        raise BudgetTimeout(t("error.budget"))

    def fetch():
        with open_url(req, timeout=min(4, left)) as response:
            return json.load(response)
    try:
        return platform_compat.call_with_deadline(fetch, left, lambda: BudgetTimeout(t("error.budget")))
    except TimeoutError as e:
        if left < 4:
            raise BudgetTimeout(t("error.budget")) from e
        raise


# what a Codex reading adds to the account's notes, given where the renewal of its login stands.
CODEX_RENEWAL_NOTES = {"started": "note.refresh_started", "dead": "err.codex_login_dead",
                       "unsaved": "err.codex_refresh_unsaved"}


def codex_reading_expiry(acc, access):
    """The expiry of the access token a reading found: its `exp`, else Codex's eight days after last_refresh."""
    exp = number((jwt_claims(access) or {}).get("exp"))
    if exp:
        return exp
    data, tokens = stored_codex_login(acc)
    return codex_expiry(data, tokens) if tokens else None


def codex_identity(acc):
    try:
        with open(os.path.join(acc["dir"], "auth.json"), encoding="utf-8") as f:
            return json.load(f)["tokens"]
    except (OSError, ValueError, KeyError):
        return {}


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()[:12] if value else None


class ReadingCache(dict):
    """Usage fetched for an email in this round can be shared, but every directory still validates its own token."""
    def __init__(self, data):
        super().__init__(data)
        self.fresh = {}


def cooldown(c, error):
    failures = c.get("failures", 0) + 1
    delay = min(900, 60 * 2 ** min(failures - 1, 4))
    header = getattr(error, "headers", None)
    retry = header.get("Retry-After") if header else None
    if retry:
        try:
            delay = max(0, float(retry))
        except ValueError:
            try:
                delay = max(0, parsedate_to_datetime(retry).timestamp() - now())
            except (TypeError, ValueError, OverflowError):
                pass
    c.update(failures=failures, retry_at=now() + delay)


def fetched(reading):
    """A missing or corrupt timestamp counts as oldest, so the next query repairs it."""
    value = reading.get("fetched")
    return value if isinstance(value, (int, float)) and math.isfinite(value) else 0


def merge_cache(key, before, reading, owner=None, strict=False):
    """Merge only what this call changed; older usage never overwrites newer, unknown fields are kept as they are.
    `strict`: only while `owner` still holds the claim (a released claim counts as lost), and only under the lock."""
    with state_lock("cache", timeout=10 if strict else 2) as locked:
        latest = load("cache.json", {})
        current = latest.get(key, {})
        if owner and current.get("inflight_owner") not in ((owner,) if strict else (None, owner)):
            return current
        if strict and not locked:
            return current
        if any(current.get(field) and before.get(field) and current[field] != before[field]
               for field in ("token_sig", "account_sig")):
            if owner and current.get("inflight_owner") == owner:
                # this call's claim is released all the same, so the new login is read at once.
                current.pop("inflight_until", None)
                current.pop("inflight_owner", None)
                latest[key] = current
                save("cache.json", latest)
            return current
        patch = {k: v for k, v in reading.items() if k not in before or before[k] != v}
        older = fetched(reading) < fetched(current)
        if older:
            for k in ("usage", "fetched", "live", "credits", "error", "notes_extra", "allowed", "limit_reached", "reset_credits", "reset_checked", "extra_usage", "extra_usage_at"):
                patch.pop(k, None)
        current = dict(current, **patch)
        for k in ("error", "retry_at", "failures", "notes_extra", "no_login", "refresh_failed", "refresh_retry_at", "refresh_dead"):
            if not older and k in before and k not in reading and current.get(k) == before[k]:
                current.pop(k, None)
        if owner:
            current.pop("inflight_until", None)
            current.pop("inflight_owner", None)
        latest[key] = current
        save("cache.json", latest)
    return current


def cached_reading(family, acc, cache, force=False):
    key = acc["key"]
    if family == "claude":
        tok, exp = claude_token(acc)
        identity = sig = fingerprint(tok)
    else:
        auth = codex_identity(acc)
        tok, exp = auth.get("access_token"), None
        identity = fingerprint(auth.get("account_id"))
        sig = fingerprint(tok)
    owner = uuid.uuid4().hex
    with state_lock("cache"):
        latest = load("cache.json", {})
        c = copy.deepcopy(latest.get(key, cache.get(key, {})))
        if "fetched" in c:
            c["fetched"] = fetched(c)
        identity_field = "token_sig" if family == "claude" else "account_sig"
        if identity and identity != c.get(identity_field):
            changed_at = now() if family == "claude" or c.get(identity_field) else 0
            # a new login drops only this tool's old readings; fields added by other programs stay.
            for field in ("usage", "fetched", "email", "profile_at", "credits", "live", "error", "no_login",
                          "retry_at", "failures", "probe_at", "notes_extra", "inflight_until", "inflight_owner",
                          "allowed", "limit_reached", "reset_credits", "reset_checked", "extra_usage", "extra_usage_at",
                          "refresh_failed", "refresh_retry_at", "refresh_dead"):
                c.pop(field, None)
            c.update({identity_field: identity, "token_changed_at": changed_at})
        if family == "codex":
            # `codex login` again on the same account keeps account_sig; a new token, or a refresh token other than
            # the refused one, ends what an earlier renewal found.
            refresh = auth.get("refresh_token")
            refresh = fingerprint(refresh) if isinstance(refresh, str) else None
            if (sig and c.get("token_sig") and sig != c["token_sig"]) or (
                    c.get("refresh_dead") and refresh and refresh != c["refresh_dead"]):
                for field in ("refresh_failed", "refresh_retry_at", "refresh_dead"):
                    c.pop(field, None)
        if sig:
            c["token_sig"] = sig
        fresh = getattr(cache, "fresh", {}).get(c.get("email"))
        if fresh and sig and family == "claude" and fetched(fresh) > fetched(c):
            c["usage"] = remember_claude_payment(c, fresh["usage"], fresh.get("extra_usage_at", fetched(fresh)))
            c.update({k: fresh[k] for k in ("fetched", "live") if k in fresh})
            for field in ("error", "retry_at", "failures"):
                c.pop(field, None)
        cooling = c.get("retry_at", 0) > now()
        flying = c.get("inflight_until", 0) > now()
        hot = c.get("usage") and (family == "claude" or c.get("live")) and now() - fetched(c) < USAGE_CACHE_SEC
        due = reset_due(c)
        query = not (cooling or flying or (hot and not force and not due) or fresh or remaining() < 1)
        if query:
            c.update(inflight_until=now() + 6, inflight_owner=owner)
            # the re-check attempt is recorded together with the in-flight claim on the verified identity, so it is not repeated even if the budget runs out.
            if due:
                c["reset_checked"] = dict(c.get("reset_checked") or {}, **due)
        if c != latest.get(key, {}):
            latest[key] = c
            save("cache.json", latest)
    if not query:
        cache[key] = c
        # a Codex login that cannot be renewed keeps saying so between queries (a Claude one has it in "error").
        if family == "codex" and c.get("refresh_failed") in ("dead", "unsaved"):
            return dict(c, notes_extra=t(CODEX_RENEWAL_NOTES[c["refresh_failed"]]))
        return c
    before = copy.deepcopy(c)
    success = False
    renewal = None
    try:
        if family == "claude" and tok and exp and exp - now() < REFRESH_MARGIN_SEC:
            # at most starts a background renewal; this reading goes on with the login as it is now.
            renewal = start_renewal(acc, c)
        elif family == "codex" and tok and not refresh_disabled("codex"):
            expiry = codex_reading_expiry(acc, tok)
            if expiry is not None and expiry - now() < CODEX_REFRESH_BEFORE_SEC:
                renewal = start_renewal(acc, c, "codex")
        if not tok or (exp and exp < now()):
            raise ValueError(t("error.no_credentials"))
        if family == "claude":
            # usage first; the profile query shares the same time budget.
            # keep two seconds for the credential re-check afterwards; the optional profile query must not eat them.
            deadline = DEADLINE.get()
            with budget(deadline - 2 if deadline is not None else None):
                c["usage"] = remember_claude_payment(c, parse_claude_usage(oauth_get("usage", tok)), now())
            c["fetched"] = now()
            c["live"] = True
            success = True

        else:
            req = urllib.request.Request(ENDPOINTS["codex_usage"],
                                         headers={"Authorization": "Bearer " + tok,
                                                  "chatgpt-account-id": auth.get("account_id", ""),
                                                  "User-Agent": "codex-cli", "Accept": "application/json"})
            r = network_json(req)
            # credits and flags were read successfully; missing windows must not erase the fact that this account is paid.
            c.update(credits=float(((r.get("credits") or {}).get("balance")) or 0),
                     allowed=(r.get("rate_limit") or {}).get("allowed"),
                     limit_reached=(r.get("rate_limit") or {}).get("limit_reached"),
                     reset_credits=(r.get("rate_limit_reset_credits") or {}).get("available_count", 0))
            usage = {}
            for w in ("primary_window", "secondary_window"):
                win = (r.get("rate_limit") or {}).get(w)
                if win:
                    name = "seven_day" if (win.get("limit_window_seconds") or 0) >= 86400 else "five_hour"
                    usage[name] = {"used": float(win["used_percent"]), "reset": float(win["reset_at"])}
            if (r.get("rate_limit") or {}).get("allowed") is False or (r.get("rate_limit") or {}).get("limit_reached") is True:
                usage.setdefault("seven_day", {"reset": None})["used"] = 100.0
            if not usage:
                raise ValueError(t("error.no_windows"))
            c.update(usage=usage, fetched=now(), live=True)
            success = True
        c["reset_checked"] = dict(c.get("reset_checked") or {}, **due)
        for field in ("error", "retry_at", "failures", "notes_extra", "no_login"):
            c.pop(field, None)
    except Exception as e:
        # this scheduled time has been tried; a failure still obeys the cooldown and the normal query interval.
        c["reset_checked"] = dict(c.get("reset_checked") or {}, **due)
        # no credentials at all in this directory: nobody is logged in there, so nothing may be sent to it.
        if tok:
            c.pop("no_login", None)
        else:
            c["no_login"] = True
        if family == "claude":
            if isinstance(e, urllib.error.HTTPError):
                c["error"] = t("err.rate_limited") if e.code == 429 else t("err.http", code=e.code)
            elif not tok or (exp and exp < now()):
                c["error"] = t("err.no_login") if not tok else renewal_error(renewal)
                if renewal == "started":
                    c["notes_extra"] = t("note.refresh_started")
            else:
                c["error"] = t("err.unreachable", error=type(e).__name__)
                log_error("usage-query", e, key)
            claude_fallback(acc, cache, c)
        else:
            fb = codex_log_reading(acc) if remaining() > 0 else {}
            if fb.get("usage") and fb.get("fetched", 0) <= c.get("token_changed_at", 0):
                fb = {}
            if fetched(fb) > fetched(c):
                c.update(fb)
                for field in ("allowed", "limit_reached"):
                    c[field] = None
            c["error"] = fb.get("error", t("err.codex_live_failed", error=type(e).__name__))
            c["live"] = False
            if remaining() >= 1 and codex_probe(acc, c, cache):
                c["notes_extra"] = t("note.probe_started")
        if isinstance(e, (OSError, TimeoutError)) and not isinstance(e, BudgetTimeout):
            if not isinstance(e, urllib.error.HTTPError) or e.code == 429 or e.code >= 500:
                cooldown(c, e)
    # the token changed during the query: the old account's result must not be stored under the new login.
    if family == "claude":
        final_sig = fingerprint(claude_token(acc)[0]) if remaining() > 0 else None
        valid = final_sig == sig
    else:
        final = codex_identity(acc)
        valid = fingerprint(final.get("access_token")) == sig and fingerprint(final.get("account_id")) == identity
    if not valid:
        # if the token cannot be re-read in time the new usage is not used, but the failure cooldown stays with the old fingerprint,
        # and is cleared together with it when a new login is noticed.
        failure = {k: c[k] for k in ("retry_at", "failures") if k in c} if not success else {}
        c = dict(before, **failure)
    if family == "codex" and renewal in CODEX_RENEWAL_NOTES:
        c["notes_extra"] = t("sep.semicolon").join(filter(None, [c.get("notes_extra"), t(CODEX_RENEWAL_NOTES[renewal])]))
    extra = c.pop("notes_extra", None)
    c = merge_cache(key, before, c, owner)
    # verified usage is saved first; a failed or timed-out profile query must not undo it.
    if success and valid and family == "claude" and (not c.get("email") or
            now() - c.get("profile_at", 0) > PROFILE_CACHE_SEC) and remaining() >= 3:
        profile_before = copy.deepcopy(c)
        try:
            deadline = DEADLINE.get()
            with budget(deadline - 2 if deadline is not None else None):
                email = oauth_get("profile", tok)["account"]["email"]
            if fingerprint(claude_token(acc)[0]) == sig:
                c = merge_cache(key, profile_before, dict(c, email=email, profile_at=now()))
        except Exception:
            pass
    # only completed queries whose identity still matches are observed; a hot cache returns without touching the observation files.
    if valid and (success or fetched(c) > fetched(before)):
        quiet_call(observe_reading, family, key, c)
    cache[key] = c
    if success and valid and c.get("email") and hasattr(cache, "fresh"):
        cache.fresh[c["email"]] = copy.deepcopy(c)
    return dict(c, notes_extra=extra) if extra else c


def claude_reading(acc, cache, force=False):
    return cached_reading("claude", acc, cache, force)


def remember_claude_payment(reading, usage, at):
    """Keep the paid-overage setting apart from the quota windows; a missing field does not mean "off", and the cache layer clears it on a new login."""
    old = (reading.get("usage") or {}).get("extra_usage")
    if "extra_usage" not in reading and isinstance(old, dict) and "is_enabled" in old:
        reading["extra_usage"] = copy.deepcopy(old)
        reading["extra_usage_at"] = fetched(reading)
    new = (usage or {}).get("extra_usage")
    if isinstance(new, dict) and "is_enabled" in new and at >= (reading.get("extra_usage_at") or 0):
        reading["extra_usage"] = copy.deepcopy(new)
        reading["extra_usage_at"] = at
    result = copy.deepcopy(usage or {})
    if "extra_usage" in reading:
        result["extra_usage"] = copy.deepcopy(reading["extra_usage"])
    return result


def claude_fallback(acc, cache, c):
    if c.get("error"):
        fb = claude_local_cache(acc)
        if fb and fb["fetched"] > max(fetched(c), c.get("token_changed_at", 0)):
            c["usage"] = remember_claude_payment(c, fb["usage"], fb["fetched"])
            c["fetched"] = fb["fetched"]
            c["live"] = False
            c["error"] = c["error"] + t("err.claude_fallback_suffix")
    cache[acc["key"]] = c
    return c


def claude_local_cache(acc):
    """The usage Claude Code last cached in its own config file (cachedUsageUtilization).
    Note: the accountUuid in there follows the config file's account label and may be stale; the numbers were fetched with that location's token."""
    cfg = os.path.join(HOME if acc["dir"] is None else acc["dir"], ".claude.json")
    try:
        with open(cfg, encoding="utf-8") as f:
            c = json.load(f)["cachedUsageUtilization"]
        return {"usage": parse_claude_usage(c["utilization"]), "fetched": c["fetchedAtMs"] / 1000}
    except Exception:
        return None


def parse_claude_usage(u):
    """five_hour and seven_day, plus the per-model weekly bucket from `limits` (kind weekly_scoped, model = the configured scarce model)."""
    out = {w: {"used": float(u[w].get("utilization") or 0), "reset": iso_to_ts(u[w].get("resets_at"))}
           for w in ("five_hour", "seven_day") if u.get(w)}
    for l in u.get("limits") or []:
        model = (((l.get("scope") or {}).get("model") or {}).get("display_name") or "")
        if SCARCE and l.get("kind") == "weekly_scoped" and model.lower() == SCARCE and l.get("resets_at"):
            out[SCARCE] = {"used": float(l.get("percent") or 0), "reset": iso_to_ts(l["resets_at"])}
    if "extra_usage" in u:
        out["extra_usage"] = copy.deepcopy(u["extra_usage"])
    return out


# ---- Codex -----------------------------------------------------------------
def codex_reading(acc, cache=None, force=False):
    """Verify the login identity first, then decide whether the cache can be reused."""
    return cached_reading("codex", acc, {} if cache is None else cache, force)


def codex_probe(acc, reading, cache):
    """Live query failed and the local log is older than CODEX_PROBE_H hours → run the cheapest possible codex exec no-op in the background so a fresh rate_limits entry gets written.
    At most once per account per CODEX_PROBE_H hours; QR_NO_PROBE=1 disables it. Returns True when a probe was actually started."""
    if os.environ.get("QR_NO_PROBE"):
        return False
    age_h = (now() - reading["fetched"]) / 3600 if reading.get("fetched") else 1e9
    last = reading.get("probe_at") or 0
    if age_h < CODEX_PROBE_H or now() - last < CODEX_PROBE_H * 3600:
        return False
    wk = (reading.get("usage") or {}).get("seven_day")
    if (reading.get("credits") or 0) > 0 and (reading.get("allowed") is False or reading.get("limit_reached") is True or not wk or
            (wk.get("reset") and wk["reset"] <= now()) or 100 - wk["used"] < SUB_FLOOR):
        return False
    try:
        lockp = os.path.join(STATE, f"probe-{acc['key']}.lock")
        # removing a stale marker and claiming it with O_EXCL both happen under the cache lock, so two processes cannot delete each other's fresh marker.
        with state_lock("cache"):
            try:
                if now() - os.stat(lockp).st_mtime >= CODEX_PROBE_H * 3600:
                    os.unlink(lockp)
            except FileNotFoundError:
                pass
            try:
                # O_EXCL is atomic on Windows too (CREATE_NEW). The mode only matters on POSIX: Windows keeps
                # nothing of it but the read-only bit, and access there follows the state directory's ACL.
                fd = os.open(lockp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
            except FileExistsError:
                return False
        logp = os.path.join(STATE, f"probe-{acc['key']}.log")
        with open(logp, "a", encoding="utf-8") as lf:
            spawn_probe(["codex", "exec", "--skip-git-repo-check", "-s", "read-only", "-C", STATE,
                         "-c", "model_reasoning_effort=low", "Reply with the single word: ok"],
                        env=dict(os.environ, CODEX_HOME=acc["dir"]), stdout=lf, stderr=lf)
        reading["probe_at"] = now()
        cache.setdefault(acc["key"], {})["probe_at"] = reading["probe_at"]
        log_decision("probe", "codex", "probe", account=acc["key"],
                     text=f"local log not updated for {min(age_h, 99999):.1f} h; probing once in the background")
        return True
    except Exception as e:
        reading["error"] = reading.get("error", "") + t("err.probe_failed_suffix", error=type(e).__name__)
        return False


def spawn_probe(argv, env, stdout, stderr):
    """Separate so tests can replace it."""
    platform_compat.spawn_detached(argv, env, stdout, stderr)


def codex_log_reading(acc):
    base = os.path.join(acc["dir"], "sessions")
    files = []
    for dp, _, fs in os.walk(base):
        if remaining() <= 0:
            break
        for f in fs:
            if remaining() <= 0:
                break
            if f.endswith(".jsonl"):
                p = os.path.join(dp, f)
                files.append((os.path.getmtime(p), p))
    files.sort(reverse=True)
    for _, p in files[:20]:
        if remaining() <= 0:
            break
        last = None
        with open(p, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if remaining() <= 0:
                    break
                if '"rate_limits"' in line:
                    last = line
        if not last:
            continue
        try:
            d = json.loads(last)
            rl = d["payload"]["rate_limits"]
            usage = {}
            for part in ("primary", "secondary"):
                win = rl.get(part)
                if win:
                    window = "seven_day" if (win.get("window_minutes") or 0) >= 1440 else "five_hour"
                    usage[window] = {"used": win["used_percent"], "reset": win["resets_at"]}
            if not usage:
                continue
            return {"usage": usage,
                    "fetched": iso_to_ts(d["timestamp"]),
                    "credits": float((rl.get("credits") or {}).get("balance") or 0),
                    "plan": rl.get("plan_type")}
        except Exception:
            continue
    return {"error": t("err.no_local_log")}


# ---- busy detection ---------------------------------------------------------
# ---- agy (Antigravity CLI) ---------------------------------------------------
# `agy -p /usage` prints one tab-separated line per bucket and window, for example
#   Gemini Models<TAB>Weekly Limit Remaining<TAB>100%<TAB>2026-10-18T13:04:19Z
# Gemini models draw on one bucket; the Claude and GPT models agy also offers draw on another.
AGY_BUCKETS = {"gemini models": "", "claude and gpt models": "partner_"}
AGY_WINDOWS = {"weekly limit remaining": "seven_day", "five hour limit remaining": "five_hour"}
AGY_PARTNER_WINDOWS = ("partner_seven_day", "partner_five_hour")
AGY_TOKEN_FILE = "antigravity-oauth-token"   # agy's login; only its fingerprint is kept, to notice a change of login


def agy_identity(acc):
    """Fingerprint of agy's login file, or None when there is none. A new login drops the old login's readings."""
    try:
        with open(os.path.join(acc["dir"] or config.default_dir("agy"), AGY_TOKEN_FILE), encoding="utf-8", errors="replace") as f:
            return fingerprint(f.read().strip())
    except OSError:
        return None


def parse_agy_usage(text):
    """The text of `agy -p /usage` → {"seven_day", "five_hour", "partner_seven_day", "partner_five_hour"}: used % and reset time.
    Lines that do not have that shape are ignored, so a changed or extended output never yields a made-up number."""
    usage = {}
    for line in (text or "").splitlines():
        parts = [part.strip() for part in line.split("\t")]
        if len(parts) != 4:
            continue
        bucket, window = AGY_BUCKETS.get(parts[0].lower()), AGY_WINDOWS.get(parts[1].lower())
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*%", parts[2])
        if bucket is None or window is None or not match:
            continue
        left = min(100.0, float(match.group(1)))
        usage[bucket + window] = {"used": 100.0 - left, "reset": iso_to_ts(parts[3])}
    return usage


AGY_QUERY_SEC = 120                # the background `agy -p /usage` may take this long (it has taken 15-64 s)
# how long a reading stays fresh, by the lower of the Gemini weekly and five-hour % left: plenty left → refresh rarely.
AGY_REFRESH_TIERS = ((50, 30 * 60), (20, 10 * 60))   # ≥50 % left: 30 min; ≥20 %: 10 min; below: USAGE_CACHE_SEC


def agy_refresh_interval(reading):
    """Seconds a reading stays fresh: longer the more quota is left. A window whose reset time has passed is not
    counted (reset_due() asks for a new reading then anyway)."""
    lefts = [100 - w["used"] for name in ("seven_day", "five_hour")
             for w in [((reading.get("usage") or {}).get(name))] if w and not (w.get("reset") and w["reset"] <= now())]
    if not lefts:
        return USAGE_CACHE_SEC
    for floor, seconds in AGY_REFRESH_TIERS:
        if min(lefts) >= floor:
            return seconds
    return USAGE_CACHE_SEC


def agy_reading(acc, cache=None, force=False):
    """agy's quota as cached. `agy -p /usage` takes far longer than a hook may wait, so it never runs here: when the
    reading is older than agy_refresh_interval() (or a window has reset, or `force`), one background process is
    started (`refresh-agy <key> <owner>`, see agy_refresh_main()) and the cached reading is returned at once. No
    second one starts while one runs (inflight_until) or during a failure cooldown."""
    cache = {} if cache is None else cache
    key = acc["key"]
    owner = uuid.uuid4().hex
    with state_lock("cache") as locked:
        latest = load("cache.json", {})
        c = copy.deepcopy(latest.get(key, cache.get(key, {})))
        if "fetched" in c:
            c["fetched"] = fetched(c)
        sig = agy_identity(acc)
        if sig and sig != c.get("account_sig"):
            for field in ("usage", "fetched", "live", "error", "no_login", "retry_at", "failures",
                          "inflight_until", "inflight_owner", "reset_checked"):
                c.pop(field, None)
            c.update(account_sig=sig, token_changed_at=now())
        cooling = c.get("retry_at", 0) > now()
        flying = c.get("inflight_until", 0) > now()
        stale = not c.get("usage") or now() - fetched(c) >= agy_refresh_interval(c)
        due = reset_due(c)
        # without the lock another process may be claiming at the same moment: use the cached numbers, change nothing.
        # A passed reset needs no marker of its own: a reading taken after it is not due any more, a failed one cools down.
        start = locked and (force or stale or due) and not (cooling or flying)
        if start:
            c.update(inflight_until=now() + AGY_QUERY_SEC + 15, inflight_owner=owner)
        if locked and (start or c != latest.get(key)):
            latest[key] = c
            save("cache.json", latest)
    if start:
        try:
            spawn_agy_refresh(acc, owner)
        except Exception as e:
            log_error("usage-query-start", e, key)
            with state_lock("cache") as released:   # release the claim so the next reading can try again
                latest = load("cache.json", {})
                if released and latest.get(key, {}).get("inflight_owner") == owner:
                    latest[key].pop("inflight_until", None)
                    latest[key].pop("inflight_owner", None)
                    save("cache.json", latest)
        c = copy.deepcopy(load("cache.json", {}).get(key, c))
        if "fetched" in c:
            c["fetched"] = fetched(c)
    cache[key] = c
    return c


def spawn_agy_refresh(acc, owner):
    """Start `python -I -X utf8 <this package>/__main__.py refresh-agy <key> <owner>` detached, its output appended
    to refresh-<key>.log in the state directory. Separate so tests can replace it."""
    with open(os.path.join(STATE, f"refresh-{acc['key']}.log"), "a", encoding="utf-8") as log:
        return platform_compat.spawn_detached([sys.executable, "-I", "-X", "utf8", ENTRY_FILE, "refresh-agy", acc["key"], owner],
                                              env=dict(os.environ), stdout=log, stderr=log)


def agy_refresh_main(argv):
    """`qr refresh-agy <key> <owner>`: the background query agy_reading() started. Not a user command."""
    acc = next((a for a in AGY_ACCOUNTS if argv and a["key"] == argv[0]), None)
    if acc is None or len(argv) < 2:
        return 2
    try:
        outcome = "ok" if agy_query(acc, argv[1]).get("live") else "failed"
    except BaseException as e:            # anything at all: one structured line, never a traceback
        log_error("usage-query", e, acc["key"])
        outcome = "failed"
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {outcome}", flush=True)
    return 0


def agy_query(acc, owner):
    """Run `agy -p /usage` once for the claim `owner` holds and merge the result into the cache. A claim another
    process took over in the meantime wins: this result is then dropped (merge_cache)."""
    key = acc["key"]
    with state_lock("cache", timeout=10) as locked:
        c = copy.deepcopy(load("cache.json", {}).get(key, {}))
    if "fetched" in c:
        c["fetched"] = fetched(c)
    if not locked or c.get("inflight_owner") != owner:
        return c
    sig = c.get("account_sig")
    before = copy.deepcopy(c)
    c.pop("no_login", None)   # a failure decides afresh whether this is a login problem
    success = False
    try:
        try:
            result = subprocess.run([AGY_PROGRAM, "-p", "/usage", "--print-timeout", f"{AGY_QUERY_SEC}s"],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    stdin=subprocess.DEVNULL, cwd=STATE, timeout=AGY_QUERY_SEC + 5)
        except FileNotFoundError:
            c["no_login"] = True
            raise ValueError(t("err.agy_missing"))
        usage = parse_agy_usage(result.stdout)
        if not usage.get("seven_day") and not usage.get("five_hour"):
            if re.search(r"log ?in|sign ?in|auth", (result.stdout or "") + (result.stderr or ""), re.I):
                c["no_login"] = True
                raise ValueError(t("err.no_login"))
            raise ValueError(t("err.agy_no_windows"))
        c.update(usage=usage, fetched=now(), live=True)
        success = True
        for field in ("error", "retry_at", "failures", "no_login"):
            c.pop(field, None)
    except Exception as e:
        c["error"] = str(e) if isinstance(e, ValueError) else t("err.unreachable", error=type(e).__name__)
        if not isinstance(e, ValueError):
            log_error("usage-query", e, key)
        c["live"] = False
        if c.get("no_login"):
            # not a server failure, so no growing backoff; but not every reading either: a new login clears it at once.
            c["retry_at"] = now() + USAGE_CACHE_SEC
        else:
            cooldown(c, e)
    # the login changed during the query: the result belongs to the old login and is not stored under the new one.
    if agy_identity(acc) != sig:
        failure = {k: c[k] for k in ("retry_at", "failures") if k in c} if not success else {}
        c, success = dict(before, **failure), False
    # strict: a query whose claim expired and was taken over (or already released) must not overwrite the newer one.
    c = merge_cache(key, before, c, owner, strict=True)
    if success:
        quiet_call(observe_reading, "agy", key, c)
    return c


def family_reading(family, acc, cache, force=False):
    """One account's reading, whatever its family."""
    if family == "claude":
        return claude_reading(acc, cache, force)
    if family == "agy":
        return agy_reading(acc, cache, force)
    return codex_reading(acc, cache, force)


NOT_SUBAGENT = ("ChatGPT.app", "CodexCLI.app", "Codex Computer Use", "app-server", "exec-server", "code-mode-host",
                "--chrome-native-host", "daemon run", "bg-pty-host", "bg-spare")


SCRIPT_HOSTS = ("node", "bun")
# one-shot agy runs are -p / --print / --prompt in any Go spelling (-p=x, -print, --prompt=x); -i is a session
EXECUTABLE_SUFFIXES = (".exe", ".cmd", ".bat", ".ps1")


def split_command_line(cmd):
    """A process command line → words. Windows command lines use backslashes as path separators and double
    quotes only (`"C:\\Program Files\\nodejs\\node.exe" C:\\…\\cli.js -p hi`), which POSIX splitting would mangle."""
    windows_style = "\\" in cmd and re.search(r'(?i)(?:^|\s)"?[a-z]:\\|\.(?:exe|cmd|bat)\b', cmd)
    try:
        if windows_style:
            return [word[1:-1] if len(word) > 1 and word[0] == word[-1] == '"' else word for word in shlex.split(cmd, posix=False)]
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


def program_name(word):
    """`/usr/bin/claude`, `C:\\Users\\me\\bin\\claude.exe`, `codex.cmd` → `claude` / `codex`."""
    name = re.split(r"[\\/]", word)[-1]
    lowered = name.lower()
    for suffix in EXECUTABLE_SUFFIXES:
        if lowered.endswith(suffix):
            return lowered[:-len(suffix)]
    return name


def script_cli(path):
    """Which CLI a script run by node is: `…/node_modules/@anthropic-ai/claude-code/cli.js` → claude,
    `…\\@openai\\codex\\bin\\codex.js` → codex, anything else → None."""
    parts = [part.lower() for part in re.split(r"[\\/]", path) if part]
    if not parts or not parts[-1].endswith((".js", ".mjs", ".cjs")):
        return None
    stem = parts[-1].rsplit(".", 1)[0]
    for family, names in (("claude", ("claude", "claude-code")), ("codex", ("codex",))):
        if stem == family or any(part in names for part in parts[:-1]):
            return family
    return None


def classify_proc(cmd):
    """One process command line → 'claude' / 'codex' / None. Only one-shot subagents count: `claude -p`, `codex exec`.
    Excluded: the resident `codex exec-server` / `app-server` of ChatGPT.app, interactive claude, daemons and
    --chrome-native-host (the exec-server used to be miscounted as a running job).
    The program may be spelled `claude`, `claude.exe`, `claude.cmd`, `codex.cmd`, or be node running the CLI's
    script (`node …\\claude-code\\cli.js -p …`), which is how the npm installs show up on Windows."""
    argv = split_command_line(cmd)
    if not argv:
        return None
    if any(x in argv[0] for x in NOT_SUBAGENT[:3]):
        return None
    exe = program_name(argv[0])
    if exe in SCRIPT_HOSTS:
        script = next((i for i, word in enumerate(argv[1:], 1) if not word.startswith("-")), None)
        exe = script_cli(argv[script]) if script else None
        if exe is None:
            return None
        argv = argv[script:]
    if any(x in ("app-server", "exec-server", "code-mode-host", "--chrome-native-host",
                 "daemon", "bg-pty-host", "bg-spare") for x in argv[1:4]):
        return None
    if exe in ("zsh", "bash", "sh", "env", "nohup", "timeout"):  # wrappers do not count; the real claude / codex underneath does
        return None
    if exe == "claude" and any(a in ("-p", "--print") or a.startswith("--print=") for a in argv[1:]):
        return "claude"
    if exe == "codex" and "exec" in argv[1:4]:
        return "codex"
    if exe in ("agy", "antigravity") and "/usage" not in argv[1:] and any(
            a.startswith("-") and a.lstrip("-").partition("=")[0] in ("p", "print", "prompt") for a in argv[1:]):
        return "agy"
    return None


def classify_session(cmd):
    """One process command line → 'claude' when it is an interactive claude session (not -p, not a subcommand,
    not a resident helper), else None. `qr launch` counts these as busy; the hook does not."""
    argv = split_command_line(cmd)
    if not argv or any(x in argv[0] for x in NOT_SUBAGENT[:3]):
        return None
    exe = program_name(argv[0])
    if exe in SCRIPT_HOSTS:
        script = next((i for i, word in enumerate(argv[1:], 1) if not word.startswith("-")), None)
        exe = script_cli(argv[script]) if script else None
        argv = argv[script:] if script else argv
    if exe != "claude" or any(a in ("-p", "--print") or a.startswith("--print=") for a in argv[1:]):
        return None
    if any(x in NOT_SUBAGENT[3:] or x in ("daemon",) for x in argv[1:4]):
        return None
    if len(argv) > 1 and re.fullmatch(r"[a-z][a-z0-9-]*", argv[1]):
        return None                      # `claude mcp …`, `claude update`: a subcommand, not a session
    return "claude"


def running_jobs():
    """{account key: number of one-shot jobs running}. Only claude -p / codex exec count (see classify_proc).
    Which account a job belongs to comes from its environment. On Windows that is not readable, so only a
    directory written on the command line (of the job or of the wrapper that started it) counts there;
    a job whose account cannot be told is left out."""
    return count_processes(classify_proc)


def running_sessions():
    """{Claude account key: number of interactive claude sessions running}, attributed like running_jobs()."""
    return count_processes(classify_session)


def count_processes(classify):
    procs = platform_compat.list_processes(run_local)
    hits = {}
    for pid, (ppid, cmd) in procs.items():
        fam = classify(cmd)
        if fam:
            hits[pid] = (fam, ppid)
    counts = {}
    for pid, (fam, ppid) in hits.items():
        if ppid in hits:  # child of a wrapper that already counted; only the outermost counts
            continue
        if fam == "agy":
            # agy has one account and no directory variable: every one-shot agy run is on it.
            if AGY_ACCOUNTS:
                counts[AGY_ACCOUNTS[0]["key"]] = counts.get(AGY_ACCOUNTS[0]["key"], 0) + 1
            continue
        variable = config.ENV_DIR[fam]
        env = platform_compat.process_environment(pid, procs[pid][1], run_local)
        if env is not None:
            matches = re.findall(r"(?:^|\s)" + variable + r"=(\S+)", env)
            value = matches[-1] if matches else None
        else:
            value = platform_compat.directory_from_command_lines(pid, procs, variable)
            if value is None:
                continue
        d = platform_compat.canonical(value) if value else None
        if fam == "claude":
            key = next((a["key"] for a in CLAUDE_ACCOUNTS
                        if (a["dir"] and platform_compat.canonical(a["dir"]) == d)), DEFAULT_KEY["claude"] if d is None else None)
        else:
            d = d or platform_compat.canonical(config.default_dir("codex"))
            key = next((a["key"] for a in CODEX_ACCOUNTS if platform_compat.canonical(a["dir"]) == d), None)
        if key:
            counts[key] = counts.get(key, 0) + 1
    return counts


def active_leases():
    leases = [l for l in load("leases.json", []) if now() - l["at"] < LEASE_SEC]
    return leases


# ---- merging directories of the same account -----------------------------------
def merge_same_account(rows):
    """The same email logged in at several directories → one row: one quota bucket, busy counts added, lead protection for the whole account.
    Representative directory: one other than the lead's (subagents should not share the lead's config directory); the first one when the lead is not in the group.
    Directories whose email is unknown are not merged."""
    groups, order = {}, []
    for r in rows:
        g = r["email"] or f"?{r['key']}"
        if g not in groups:
            groups[g] = []
            order.append(g)
        groups[g].append(r)
    out = []
    for g in order:
        rs = groups[g]
        if len(rs) == 1:
            out.append(rs[0])
            continue
        rep = next((r for r in rs if not r["is_lead"]), rs[0])
        fresh = max(rs, key=lambda r: (r["usage"] is not None, r["fetched"] or 0))
        m = dict(rep)
        m.update({"usage": fresh["usage"], "fetched": fresh["fetched"], "error": fresh["error"],
                  "live": fresh.get("live"), "notes_extra": fresh.get("notes_extra"), "retry_at": fresh.get("retry_at"),
                  "credits": fresh["credits"], "notes": list(fresh["notes"]),
                  "keys": [r["key"] for r in rs], "dirs": [r["dir"] for r in rs],
                  "jobs": sum(r["jobs"] for r in rs), "n_lease": sum(r["n_lease"] for r in rs),
                  "is_lead": any(r["is_lead"] for r in rs)})
        for field in ("allowed", "limit_reached", "reset_credits", "inflight_until", "no_login"):
            m[field] = fresh.get(field)
        known_payment = [r for r in rs if "is_enabled" in ((r.get("usage") or {}).get("extra_usage") or {})]
        if known_payment:
            payment = max(known_payment, key=lambda r: r.get("extra_usage_at") or r["fetched"] or 0)
            m["usage"] = copy.deepcopy(m["usage"] or {})
            m["usage"]["extra_usage"] = copy.deepcopy(payment["usage"]["extra_usage"])
            m["extra_usage_at"] = payment.get("extra_usage_at") or payment["fetched"]
        m["notes"].append(t("note.merged", dirs=t("sep.plus").join(dir_label(r["dir"]) for r in rs)))
        out.append(m)
    return out


def find_row(rows, key):
    """The key may be an alias that was merged into another row."""
    return next((r for r in rows if key in r.get("keys", [r["key"]])), None)


# ---- scoring ----------------------------------------------------------------
def read_family(family, force=False, lead_key=None):
    """One row per account. For family=claude, directories with the same email are merged into one row (see merge_same_account).
    lead_key: the account the lead runs on; detected when omitted."""
    accs = accounts(family)
    if family == "claude" and lead_key is None:
        lead_key = detect_lead()[0]
    cache = ReadingCache(load("cache.json", {}))
    jobs = running_jobs()
    leases = active_leases()
    rows = []
    readings = {}
    for a in sorted(accs, key=lambda a: fetched(cache.get(a["key"], {}))):
        readings[a["key"]] = family_reading(family, a, cache, force)
    for a in accs:
        r = readings[a["key"]]
        # each row keeps the state of its own reading.
        row = {"key": a["key"], "dir": a["dir"], "email": r.get("email", ""), "notes": [],
               "usage": r.get("usage"), "fetched": fetched(r), "error": r.get("error"),
               "credits": r.get("credits"), "keys": [a["key"]], "dirs": [a["dir"]],
               "live": bool(r.get("live")), "notes_extra": r.get("notes_extra"), "retry_at": r.get("retry_at"),
               "allowed": r.get("allowed"), "limit_reached": r.get("limit_reached"),
               "reset_credits": r.get("reset_credits", 0), "inflight_until": r.get("inflight_until"),
               "extra_usage_at": r.get("extra_usage_at"), "no_login": bool(r.get("no_login")),
               "is_lead": family == "claude" and a["key"] == lead_key}
        row["jobs"] = jobs.get(a["key"], 0)
        row["n_lease"] = sum(1 for l in leases if l["key"] == a["key"] and now() - l["at"] < LEASE_BUSY_SEC)
        rows.append(row)
    if family == "claude":
        rows = merge_same_account(rows)
    return rows


def model_class(model=None, family="claude"):
    """Which quota bucket a dispatch draws on. agy: "partner" for the Claude and GPT models it offers, else Gemini ("other")."""
    if family == "agy":
        return "partner" if model and (model == "partner" or model.lower().startswith(("claude", "gpt"))) else "other"
    return SCARCE if SCARCE and family == "claude" and (model is None or SCARCE in model.lower()) else "other"


def reading_fresh(row, family=None):
    """A live reading still within the time it counts as current: USAGE_CACHE_SEC, or for agy its refresh interval
    (agy is read in the background and less often the more quota is left)."""
    limit = agy_refresh_interval(row) if family == "agy" else USAGE_CACHE_SEC
    return bool(row.get("live") and row.get("usage") and 0 <= now() - fetched(row) <= limit)


def reset_due(reading):
    """Each scheduled reset of each window is re-checked once; it is recorded only for a verified identity, and a cooldown still wins."""
    checked = reading.get("reset_checked") or {}
    return {name: win["reset"] for name, win in (reading.get("usage") or {}).items()
            if (name in windows() or name in AGY_PARTNER_WINDOWS) and win and win.get("reset")
            and fetched(reading) < win["reset"] <= now() and checked.get(name) != win["reset"]}


def not_logged_in(row):
    """No credentials were found in the account's directory and there is no reading from an earlier login either."""
    return bool(row.get("no_login") and not row.get("usage"))


def credit_state(row):
    u = row.get("usage") or {}
    row["paid"] = ((row.get("credits") or 0) > 0 if row["family"] == "codex"
                   else bool((u.get("extra_usage") or {}).get("is_enabled")))
    # Codex says through its live flags whether the subscription still covers a dispatch. A reading without them
    # (the local fallback) cannot tell, so any window at 0% with a credit balance counts as "would charge".
    unflagged = row["family"] == "codex" and row.get("allowed") is None and row.get("limit_reached") is None
    row["will_spend"] = row["paid"] and (row["wk_left"] == 0 or
                           (row["family"] == "claude" or unflagged) and row["fh_left"] == 0)
    wins = [u.get("seven_day")]
    if row["model"] != "other" and u.get(row["model"]):
        wins.append(u[row["model"]])
    row["pay_risk"] = row["paid"] and (not u.get("seven_day") or any(
        w and 100 - w["used"] < SUB_FLOOR and (w.get("reset") is None or w["reset"] > now()) for w in wins))


def spend_note(family):
    return t("note.will_charge_credits") if family == "codex" else t("note.will_incur_overage")


def credit_block(row):
    return bool(row.get("paid") and not row.get("allow_credits") and
                (row.get("will_spend") or row.get("pay_risk")))


def evaluate(family, force=False, lead_key=None, model=None, allow_credits=None, raw=None):
    """Readings can be shared; scoring for a model and a credit policy makes no external query."""
    if raw is None:
        raw = read_family(family, force, lead_key)
    return score_rows(family, raw, model, allow_credits)


def five_hour_projection(row):
    """The five-hour window after the jobs already running on the account: each is assumed to still take
    INFLIGHT_FH_COST % of it. A burst of dispatches cannot read the same roomy number six times and empty the window."""
    if row.get("fh_left") is None:
        return None
    return max(0.0, row["fh_left"] - row.get("busy", 0) * INFLIGHT_FH_COST)


def five_hour_block(row):
    """The five-hour rule for one row (None when it does not block): the general floor, and the lead's own, higher one.
    The message is kept in row["fh_block"] so refresh_busy() can recompute it when the busy count changes."""
    proj = five_hour_projection(row)
    row["fh_proj"] = proj
    msg = None
    if proj is not None:
        floor = LEAD_FH_FLOOR if row.get("is_lead") else FIVE_HOUR_FLOOR
        if proj < floor:
            if row.get("is_lead"):
                msg = t("blocked.lead_five_hour", left=proj)
            elif proj < row["fh_left"]:
                msg = t("blocked.five_hour_projected", left=proj, raw=row["fh_left"], jobs=row.get("busy", 0))
            else:
                msg = t("blocked.five_hour", left=proj)
    if row.get("fh_note") in row["notes"]:
        row["notes"].remove(row["fh_note"])
    row["fh_note"] = None
    if msg is None and proj is not None and proj < row["fh_left"]:
        row["fh_note"] = t("note.inflight_projection", raw=row["fh_left"], jobs=row.get("busy", 0), left=proj)
        row["notes"].append(row["fh_note"])
    row["fh_block"] = msg
    return msg


def score_rows(family, raw, model=None, allow_credits=None):
    rows = copy.deepcopy(raw)
    category = model_class(model, family)
    if allow_credits is None:
        allow_credits = ALLOW_CREDITS or os.environ.get("QR_ALLOW_CREDITS") == "1"
    # the readings already carry the reservation counts; pure computation, no state file is re-read.
    leases = []
    for row in rows:
        leases.extend({"key": row["key"], "at": now()} for _ in range(row["n_lease"]))
    # one timestamp for every row's "hours until reset": rows with identical readings then score exactly the same,
    # and the tie goes to the account listed first in the config file instead of to whichever row the clock favoured.
    scored_at = now()
    for row in rows:
        row.update(family=family, model=category, allow_credits=allow_credits, fresh=reading_fresh(row, family))
        row["busy"] = max(row["jobs"], row["n_lease"])
        row["busy_detail"] = t("busy.detail", jobs=row["jobs"], leases=row["n_lease"])
        u = row["usage"] or {}
        if family == "agy" and category == "partner":
            u = {name: u.get("partner_" + name) for name in ("seven_day", "five_hour")}
        wk = u.get("seven_day")
        if wk and wk.get("reset") and wk["reset"] <= now():
            row["notes"].append(t("note.reset_passed"))
        fh = u.get("five_hour")
        age_h = (now() - row["fetched"]) / 3600 if row["fetched"] else None
        row["age_h"] = age_h
        stale_codex = family == "codex" and not row.get("live") and age_h is not None and age_h > CODEX_STALE_H
        unknown = wk is None or stale_codex
        if stale_codex:
            row["notes"].append(t("note.stale_codex"))
        if row.get("notes_extra"):
            row["notes"].append(row["notes_extra"])
        if (row.get("retry_at") or 0) > now():
            row["notes"].append(t("note.cooldown", seconds=math.ceil(row["retry_at"] - now())))
        if wk and wk.get("reset") and wk["reset"] <= now():
            wk = None
            unknown = True
        fb = u.get(category) if category != "other" else None
        row["wk_source"] = "shared"
        if wk and fb and (fb.get("reset") or 0) > now() and fb["used"] > wk["used"]:
            wk = fb
            row["wk_source"] = category
            row["notes"].append(t("note.scarce_tighter", model=SCARCE_LABEL))
        row["wk_left"] = None if wk is None else max(0.0, 100 - wk["used"])
        row["wk_reset"] = wk and wk["reset"]
        if family == "codex" and (row.get("allowed") is False or row.get("limit_reached") is True):
            row["wk_left"] = 0.0
            unknown = False
        if fh and fh.get("reset") and fh["reset"] <= now():
            fh = None
        row["fh_left"] = None if fh is None else max(0.0, 100 - fh["used"])
        row["fh_reset"] = fh and fh["reset"]
        if row["error"]:
            row["notes"].append(row["error"])
        if age_h is not None and age_h > 0.1:
            row["notes"].append(t("note.age_hours", n=age_h) if age_h >= 1 else t("note.age_minutes", n=age_h * 60))
        credit_state(row)
        # quota available per hour
        if unknown:
            row["rate"] = None
            row["notes"].append(t("note.unknown"))
        else:
            hrs = max(((row["wk_reset"] or scored_at + 7 * 86400) - scored_at) / 3600, 0.25)
            row["rate"] = row["wk_left"] / hrs
            if row["wk_left"] is not None and row["wk_left"] <= 0 and row["paid"] and allow_credits:
                row["rate"] = 0.01
                row["notes"].append(spend_note(family))
        # hard exclusions
        row["blocked"] = five_hour_block(row)
        if row.get("is_lead"):
            if row["wk_left"] is None:
                row["blocked"] = t("blocked.lead_unknown")
            elif row["wk_left"] < LEAD_FLOOR:
                row["blocked"] = t("blocked.lead_low", left=row["wk_left"])
        if row["wk_left"] is not None and row["wk_left"] <= 0 and not (row["paid"] and allow_credits):
            row["blocked"] = t("blocked.scarce_out", model=SCARCE_LABEL) if row["wk_source"] != "shared" else t("blocked.weekly_out")
        elif (not row.get("is_lead") and row["wk_left"] is not None and 0 < row["wk_left"] < SUB_FLOOR
              and not row["blocked"]):
            if row["paid"] and allow_credits:
                row["notes"].append(t("note.near_credits", floor=SUB_FLOOR))
                row["rate"] = min(row["rate"] or 0.01, 0.01)
            else:
                row["blocked"] = (t("blocked.low_no_credits", left=row["wk_left"]) if row["paid"] else
                                  t("blocked.below_floor", left=row["wk_left"], floor=SUB_FLOOR))
        if row["paid"] and allow_credits and row["will_spend"] and not row["is_lead"]:
            row["blocked"] = None
            row["rate"] = min(row["rate"] or 0.01, 0.01)
            notice = spend_note(family)
            if notice not in row["notes"]:
                row["notes"].append(notice)
        if not allow_credits and row["paid"]:
            if row["will_spend"]:
                row["blocked"] = t("blocked.weekly_out_no_credits") if row["wk_left"] == 0 else t("blocked.five_hour_out_no_credits")
            elif row["pay_risk"]:
                if not row["fresh"] or row["wk_left"] is None:
                    row["blocked"] = t("blocked.stale_pay_risk")
                elif not row["blocked"]:
                    row["blocked"] = t("blocked.low_no_credits", left=row["wk_left"])
        if not_logged_in(row):
            row["blocked"] = t("blocked.no_login")
        if unknown and wk and (row["blocked"] or row["rate"] is not None):
            row["notes"].remove(t("note.unknown"))
            row["notes"].append(t("note.old_nearly_out_hold"))
    for row in rows:
        # what blocked the row apart from the five-hour rule; refresh_busy() redoes only that rule when the busy count changes.
        row["blocked_other"] = None if row["blocked"] and row["blocked"] == row.get("fh_block") else row["blocked"]
    refresh_busy(rows, leases)
    return rows


def refresh_busy(rows, leases, pending=()):
    """Inside the lock only reservations and scores are recomputed; every key of a merged row counts."""
    for row in rows:
        row["n_lease"] = sum(1 for l in leases if l["key"] in row["keys"] and now() - l["at"] < LEASE_BUSY_SEC)
        added = sum(1 for l in pending if l["key"] in row["keys"])
        row["busy"] = max(row["jobs"], row["n_lease"] - added) + added
        row["busy_detail"] = t("busy.detail", jobs=row["jobs"], leases=row["n_lease"])
        if "fh_block" in row and not row.get("blocked_other"):
            row["blocked"] = five_hour_block(row)
    known = [r["rate"] for r in rows if r["rate"] is not None]
    mid = statistics.median(known) if known else 1.0
    for r in rows:
        base = r["rate"] if r["rate"] is not None else mid
        fh = r.get("fh_proj", r["fh_left"])
        if fh is not None and fh < 30:
            base *= fh / 30
        r["score"] = base / (1 + r["busy"])
    return rows


def prefix(family, row):
    """Shell text that selects an account; the directory is quoted, so use it with `eval` or paste it, see
    "Using `qr pick` in scripts" in docs/REFERENCE.md."""
    if family == "agy":
        return ""   # agy has one account and no directory variable: nothing to select
    if family == "claude":
        return ("env -u CLAUDE_CONFIG_DIR" if row["dir"] is None
                else "CLAUDE_CONFIG_DIR=" + platform_compat.shell_quote(row["dir"]))
    return "CODEX_HOME=" + platform_compat.shell_quote(row["dir"])


def who(row):
    return (row["email"].split("@")[0] if row["email"] else row["key"])


def reason(row):
    bits = []
    if row["wk_left"] is not None:
        bits.append(t("reason.weekly", left=row["wk_left"], when=fmt_left(row["wk_reset"])))
    if row["fh_left"] is not None:
        bits.append(t("reason.five_hour", left=row["fh_left"]))
    if row["busy"]:
        bits.append(t("reason.busy", jobs=row["jobs"], leases=row["n_lease"]))
    return t("reason.line", who=who(row), details=t("sep.comma").join(bits + row["notes"]))


def add_lease(key, session=""):
    with state_lock("leases"):
        leases = active_leases()
        leases.append({"key": key, "at": now(), "session": session})
        save("leases.json", leases)


def command_hash(cmd):
    return hashlib.sha256(cmd.encode("utf-8", "replace")).hexdigest()[:12] if cmd else "-"


def log_reason(row):
    """The numbers behind a decision, always in English; notes are left out because they follow the display language."""
    bits = []
    if row.get("wk_left") is not None:
        bits.append(f"weekly {row['wk_left']:.0f}% left")
    if row.get("fh_left") is not None:
        bits.append(f"five-hour {row['fh_left']:.0f}% left")
    if row.get("busy"):
        bits.append(f"{row.get('jobs', 0)} running, {row.get('n_lease', 0)} reserved")
    return ", ".join(bits)


LOG_SOURCES = ("hook", "qr pick", "qr check", "probe", "qr launch", "refresh")
LOG_DETAILS = re.compile("|".join((
    "kept the original account", "conversation location unknown", "named a directory that is not in the pool",
    "as specified", "as specified, although it is blocked", "dispatched with a warning",
    r"local log not updated for \d+\.\d h; probing once in the background",
    # refresh (a login renewed by quota-router), REFRESH_TEXT / CODEX_REFRESH_TEXT; a failure may name the HTTP status
    # code; the [notify] command's outcome, NOTIFY_TEXT
    *(re.escape(text) for text in dict.fromkeys([*REFRESH_TEXT.values(), *CODEX_REFRESH_TEXT.values(),
                                                 *NOTIFY_TEXT.values()])), r"renewal failed, HTTP \d{3}",
    # qr launch
    "launched with a warning", "on paid overage", "no account can take it", "no account could be read",
    "no account is set up for sessions")))


def log_decision(source, family, mode, row=None, text="", cmd="", lead=None, session="", model=None, account=None):
    """One line per routing decision in decisions.log; this is where to look for "where did it go".

    Every field is an account key, a family, a model class, a fixed English phrase from this file or a number.
    The command is present only as a hash, the account only as its key (never the email or the directory), and
    a key, lead or session id with characters outside `[A-Za-z0-9_.-]` as a hash. `text` is one of LOG_DETAILS,
    an exception class name for mode "fail", or for a deny the message shown to the lead, of which only the
    reason category is kept; any other text is recorded as "other"."""
    try:
        bits = []
        if mode == "deny":
            bits.append("blocked: " + i18n.EN["deny." + decision_code(text)])
        elif text:
            known = LOG_DETAILS.fullmatch(text) or mode == "fail" and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", text)
            bits.append(text if known else "other")
        if row:
            if mode != "deny" and log_reason(row):
                bits.append(log_reason(row))
            model = row.get("model", "other")
        if model:
            bits.append(f"model {log_token(model)}")
        if row and row.get("will_spend") and row.get("allow_credits") and mode not in ("deny", "fail"):
            bits.append("credits allowed")
        who_ = log_token(row["key"] if row else account)
        lead_, assumed = (lead[:-len(" (assumed)")], " (assumed)") if lead and lead.endswith(" (assumed)") else (lead, "")
        lead_ = (log_token(lead_) if lead_ else "?") + assumed
        family_ = family if family in config.FAMILIES + ("?",) else "other"
        # the first characters of a session id of the usual shape; one of any other shape only as a hash.
        session_ = session[:8] if LOG_TOKEN.fullmatch(session or "") else log_token(session)
        with open(os.path.join(STATE, "decisions.log"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%m-%d %H:%M:%S} | {source if source in LOG_SOURCES else 'other'} | lead={lead_} | "
                    f"sess={session_} | {family_} | {log_token(mode)} | pick={who_} | {'; '.join(bits)} | "
                    f"cmd#{command_hash(cmd)}\n")
    except Exception:
        pass


def stale_warning(row):
    age = row.get("age_h")
    text = t("age.unknown") if age is None else (t("age.minutes", n=age * 60) if age < 1 else t("age.hours", n=age))
    return t("warn.stale", age=text)


def reset_options(rows):
    """Earliest scheduled time a block could lift: per row the latest reset among the windows that block it, then the earliest across rows.
    Windows that are not blocking (a roomy five-hour window, say) are left out."""
    items = []
    for r in rows:
        floor_fh = LEAD_FH_FLOOR if r.get("is_lead") else FIVE_HOUR_FLOOR
        floor_wk = LEAD_FLOOR if r.get("is_lead") else SUB_FLOOR
        fh_block = r.get("fh_left") is not None and r["fh_left"] < floor_fh
        wk_block = r.get("wk_left") is not None and r["wk_left"] < floor_wk
        cands = []
        if fh_block and r.get("fh_reset") and r["fh_reset"] > now():
            cands.append((r["fh_reset"], t("window.five_hour")))
        if (wk_block or not fh_block) and r.get("wk_reset") and r["wk_reset"] > now():
            cands.append((r["wk_reset"], t("window.scarce_weekly", model=SCARCE_LABEL) if r.get("wk_source", "shared") != "shared" else t("window.shared_weekly")))
        if cands:
            ts, window = max(cands)
            items.append((ts, r["key"], window))
    if not items:
        return t("reset.earliest_unknown")
    ts, key, window = min(items)
    return t("reset.earliest", key=key, window=window, clock=fmt_clock(ts))


_TEXT_CONSTANTS = {"CREDIT_END": "credit.end", "BLOCK_END": "block.end", "CREDIT_HOW": "credit.how"}


def __getattr__(name):
    """CREDIT_END / BLOCK_END / CREDIT_HOW follow the current language."""
    if name in _TEXT_CONSTANTS:
        return t(_TEXT_CONSTANTS[name])
    raise AttributeError(name)


def credit_line(row):
    """One line per account: why it would cost money, the balance, any reset vouchers."""
    balance = t("credit.balance", credits=row.get("credits") or 0) if row["family"] == "codex" else t("credit.overage_on")
    why = (t("credit.why_codex_spend") if row["family"] == "codex" else t("credit.why_claude_spend")) if row["will_spend"] else t("credit.why_risk")
    line = t("credit.line", key=row["key"], why=why, balance=balance)
    if row["family"] == "codex" and row.get("reset_credits"):
        line += t("credit.vouchers", count=row["reset_credits"])
    return line


def credit_message(row):
    return "\n".join([credit_line(row), reset_options([row]), t("credit.how"), t("credit.end")])


def blocked_message(rows):
    parts = [t("block.none")]
    for row in rows:
        parts.append(credit_line(row) if credit_block(row) else
                     t("block.row", key=row["key"], reason=row["blocked"] or t("block.no_reading")))
        if (row.get("retry_at") or 0) > now():
            parts.append(t("block.cooling", key=row["key"], clock=fmt_clock(row["retry_at"])))
    parts.append(reset_options(rows))
    if any(credit_block(r) for r in rows):
        parts.append(t("credit.how"))
    parts.append(t("block.end"))
    if any(credit_block(r) for r in rows):
        parts.append(t("credit.end"))
    return "\n".join(parts)


def retry_blocked(family, raw, model=None, allow_credits=None, attempted=None):
    """Only when every row is blocked are the stale rows re-queried; external queries must happen outside the lease lock."""
    rows = score_rows(family, raw, model, allow_credits)
    if any(not r["blocked"] for r in rows):
        return raw
    attempted = set() if attempted is None else attempted
    cache = ReadingCache(load("cache.json", {}))
    accs = accounts(family)
    for row in rows:
        if (row["fresh"] or (row.get("retry_at") or 0) > now() or
                (row.get("inflight_until") or 0) > now() or remaining() < 1):
            continue
        key = row["key"]
        if key in attempted:
            continue
        attempted.add(key)
        acc = next(a for a in accs if a["key"] == key)
        r = family_reading(family, acc, cache, True)
        target = find_row(raw, key)
        if family == "claude":
            r = dict(r, usage=remember_claude_payment(target, r.get("usage"), r.get("extra_usage_at") or fetched(r)))
        for field in ("usage", "fetched", "error", "credits", "live", "notes_extra", "retry_at", "inflight_until", "allowed", "limit_reached", "reset_credits", "no_login"):
            target[field] = r.get(field)
        target["fetched"] = fetched(r)
    return raw


def choose(family, rows=None, force=False):
    """Returns (best account or None, warning, all rows); pure computation when rows are passed in."""
    if rows is None:
        raw = read_family(family, force)
        rows = score_rows(family, retry_blocked(family, raw))
    # sorted() and min() are stable and the rows are in config-file order, so equal scores keep that order.
    order = lambda r: (bool(r.get("will_spend")), bool(LEAD_LAST_RESORT and r.get("is_lead")), -r["score"])
    ok = sorted([r for r in rows if not r["blocked"]], key=order)
    if ok:
        best = ok[0]
        warn = t("warn.all_tight", reset=reset_options(rows)) if best["wk_left"] is not None and best["wk_left"] < TIGHT_WARN else None
        return best, warn, rows
    fallback = [r for r in rows if not r.get("fresh", True) and not r.get("is_lead") and not credit_block(r)
                and not not_logged_in(r)]
    if fallback:
        best = min(fallback, key=lambda r: (*order(r), r["busy"]))
        best["notes"] = [n.replace(t("note.old_nearly_out_hold"), t("note.old_nearly_out")) for n in best["notes"]]
        return best, "⚠ " + stale_warning(best), rows
    return None, blocked_message(rows), rows


def cross_hint(family, rows, force=False, best=None, orows=None):
    """Cross-family hint, or None. The choice of family stays with the lead. Two triggers:
    1. this family's best dispatchable weekly < CROSS_HINT_MINE and the other's best ≥ CROSS_HINT_OTHER (or ≥ double);
    in between: a weekly window of the other family expires within 24 hours with at least 30% left;
    2. any account of the other family has weekly ≥ CROSS_IDLE_OTHER with nothing running or just sent, while the one picked here has ≥ CROSS_BUSY_MINE jobs or at most half that quota."""
    ok = [r for r in rows if not r["blocked"] and r["wk_left"] is not None]
    mine_row = best if best is not None else (max(ok, key=lambda r: r["wk_left"]) if ok else None)
    mine = max((r["wk_left"] for r in ok), default=0)
    if orows is None:
        return None
    cand = [r for r in orows if not r["blocked"] and r["wk_left"] is not None]
    if not cand:
        return None
    other = cand[0].get("family") or ("codex" if family == "claude" else "claude")
    top = max(cand, key=lambda r: (r["wk_left"], -r["busy"]))
    me, them = LABELS[family], LABELS[other]
    verb = VERBS[other]
    if mine < CROSS_HINT_MINE and (top["wk_left"] >= CROSS_HINT_OTHER or top["wk_left"] >= 2 * mine):
        return t("hint.cross_low", me=me, mine=mine, them=them, who=who(top), left=top["wk_left"], verb=verb)
    expiry = quiet_call(expiring_hint, orows)
    if expiry:
        return expiry
    idle = [r for r in cand if r["wk_left"] >= CROSS_IDLE_OTHER and r["jobs"] == 0 and r["n_lease"] == 0]
    if idle:
        top = max(idle, key=lambda r: r["wk_left"])
    if mine_row is not None and idle and \
            (mine_row["busy"] >= CROSS_BUSY_MINE or (mine_row["wk_left"] or 0) <= top["wk_left"] / 2):
        why = (t("hint.why_busy", busy=mine_row["busy"]) if mine_row["busy"] >= CROSS_BUSY_MINE else
               t("hint.why_unknown") if mine_row["wk_left"] is None else t("hint.why_low", left=mine_row["wk_left"]))
        return t("hint.cross_idle", them=them, who=who(top), left=top["wk_left"], me=me, mine_who=who(mine_row),
                 why=why, verb=verb)
    return None


def cross_hints(family, rows, best=None, others=None):
    """The first cross-family hint among the other families, the roomiest family first. others: {family: scored rows}."""
    def roomiest(item):
        return max((r["wk_left"] for r in item[1] if not r["blocked"] and r["wk_left"] is not None), default=-1)
    for _, orows in sorted((others or {}).items(), key=roomiest, reverse=True):
        if orows:
            hint = cross_hint(family, rows, best=best, orows=orows)
            if hint:
                return hint
    return None


def family_summary(family, rows):
    """One-line summary of another family, attached to every dispatch: account, weekly left, busy, and how old the numbers are."""
    label = LABELS[family]
    parts, ages = [], []
    for r in rows:
        shared = (r.get("usage") or {}).get("seven_day")
        left = max(0, 100 - shared["used"]) if family == "claude" and shared else r["wk_left"]
        wk = f"{left:.0f}%" if left is not None else "?"
        reset = shared.get("reset") if shared else r.get("wk_reset")
        countdown = ((t("summary.reset_passed") if reset <= now() else t("summary.reset_in", when=fmt_left(reset)))
                     if reset else t("summary.reset_unknown"))
        flag = t("summary.maybe_reset") if any(t("marker.maybe_reset") in n for n in r["notes"]) else ""
        parts.append(t("summary.account", who=who(r), left=wk, countdown=countdown, flag=flag, jobs=r["jobs"]))
        if r.get("age_h") is not None:
            ages.append(r["age_h"])
    age = max(ages) if ages else None
    # no reading at all is not a live one.
    age_s = t("age.unknown") if age is None else t("age.live") if age < 0.2 else (t("age.minutes", n=age * 60) if age < 1 else t("age.hours", n=age))
    return t("summary.line", label=label, accounts=t("sep.slash").join(parts), age=age_s)


def pick(family, peek=False, env_only=False, force=False, model=None, strict=True):
    """`strict` is accepted and ignored: --env has always-strict behaviour, the flag is kept so scripts that pass it keep working."""
    lead_key, assumed = detect_lead()
    allow = ALLOW_CREDITS or os.environ.get("QR_ALLOW_CREDITS") == "1"
    raw = read_family(family, force, lead_key)
    raw = retry_blocked(family, raw, model, allow)
    rows = score_rows(family, raw, model, allow)
    others = {}
    for other in other_families(family):
        try:
            others[other] = evaluate(other, force, model="other")
        except Exception:
            pass
    with state_lock("leases"):
        leases = active_leases()
        refresh_busy(rows, leases)
        best, warn, _ = choose(family, rows)
        if best is not None and not peek:
            leases.append({"key": best["key"], "at": now(), "session": ""})
            save("leases.json", leases)
    extras = [qrtext for qrtext in [cross_hints(family, rows, best=best, others=others)] +
              [family_summary(other, orows) for other, orows in others.items() if orows] if qrtext]
    if family == "claude" and model is None and SCARCE:
        extras.insert(0, t("note.no_model", model=SCARCE_LABEL))
    notices = quiet_call(recent_notices, limit=3, default=[])
    if best is None:
        for notice in notices:
            print(notice, file=sys.stderr)
        message = append_context(warn, extras)
        if not peek:
            log_decision("qr pick", family, "deny", text=message, lead=lead_key, model=model_class(model, family))
        print(message, file=sys.stderr)
        raise SystemExit(3)
    if not peek:
        log_decision("qr pick", family, "pick", best, lead=(lead_key or "?") + (" (assumed)" if assumed else ""))
    print(prefix(family, best) if env_only else f"{prefix(family, best)}   # {reason(best)}")
    if family == "claude" and assumed and not env_only:
        print(t("pick.assumed"), file=sys.stderr)
    for text in notices + ([warn] if warn else []) + extras:
        print(text, file=sys.stderr if env_only else sys.stdout)


def append_context(message, extras):
    """Add context lines while keeping the mandatory closing warnings at the end."""
    tail = []
    for ending in (t("credit.end"), t("block.end")):
        if message.endswith(ending):
            message = message[:-len(ending)].rstrip()
            tail.insert(0, ending)
    return "\n".join([message] + extras + tail)


def account_key(family, value):
    accs = accounts(family)
    for a in accs:
        if value == a["key"]:
            return a["key"]
    if family in ("codex", "agy") and value.isdigit():  # 1, 2, …: the n-th account of the family in the config file
        return accs[int(value) - 1]["key"] if 0 < int(value) <= len(accs) else None
    if family == "claude":
        return dir_key(value.replace("${HOME}", HOME))
    path = platform_compat.canonical(value.replace("${HOME}", HOME).replace("$HOME", HOME))
    return next((a["key"] for a in accs if platform_compat.canonical(a["dir"]) == path), None)


def check_account(family, account, model=None, peek=False, force=False):
    key = account_key(family, account)
    rows = evaluate(family, force, model=model)
    row = find_row(rows, key)
    if row is None or row["blocked"]:
        message = t("check.not_in_pool") if row is None else (credit_message(row) if credit_block(row) else blocked_message([row]))
        if not peek:
            log_decision("qr check", family, "deny", row, text=message, model=model_class(model, family))
        print(message, file=sys.stderr)
        raise SystemExit(3)
    # a directory named explicitly keeps its own directory and key even when its row was merged.
    row = dict(row, key=key, dir=next(a["dir"] for a in accounts(family) if a["key"] == key))
    if not peek:
        add_lease(key)
        log_decision("qr check", family, "check", row)
    print(f"{prefix(family, row)}   # {reason(row)}")


BAR_WIDTH = 20


def display_width(text):
    """Terminal columns a string takes: CJK and other wide characters count 2."""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def pad(text, width):
    """Left-align `text` in `width` terminal columns (CJK-aware)."""
    return text + " " * max(0, width - display_width(text))


def bar(left, width=BAR_WIDTH):
    """`▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱▱▱▱▱▱  43/100%` for a percentage left; an unknown value is an empty bar with `?`."""
    if left is None:
        return "\u25b1" * width + "   ?/100%"
    left = min(100, max(0, left))
    filled = int(round(left / 100 * width))
    return "\u25b0" * filled + "\u25b1" * (width - filled) + f"  {left:.0f}/100%"


def status(force=False, bars=False):
    failures = recent_failures()
    if failures:
        print(t("fail.recent", count=failures))
    lead_key, assumed = detect_lead()
    for fam in managed_families():
        raw = read_family(fam, force, lead_key)
        if fam in ("claude", "codex") and force and RENEWALS and interactive():
            # a person asked for fresh numbers: wait for the logins this started renewing, then read again.
            print(t("status.renewal_wait", keys=t("sep.list").join(sorted({key for key, _ in RENEWALS})),
                    seconds=REFRESH_WAIT_SEC), file=sys.stderr, flush=True)
            if wait_for_renewals():
                raw = read_family(fam, False, lead_key)
        rows = score_rows(fam, raw, model="other")
        fable_rows = score_rows(fam, raw) if fam == "claude" and SCARCE else []
        print(f"\n[{fam}]")
        if fam == "claude":
            lr = find_row(rows, lead_key)
            ld = next((a["dir"] for a in CLAUDE_ACCOUNTS if a["key"] == lead_key), None)
            print(t("status.lead", who=who(lr) if lr else lead_key or "?", dir=dir_label(ld),
                    assumed=t("status.assumed") if assumed else "", floor=LEAD_FLOOR, floor_fh=LEAD_FH_FLOOR))
        col = lambda name: t("status.col." + name)
        if not bars:
            print(f"{col('account'):<16}{col('dir'):<24}{col('weekly'):>6}{col('weekly_reset'):>14}{col('five_hour'):>9}"
                  f"{col('rate'):>10}{col('busy'):>10}  {col('state')}")
        for r in sorted(rows, key=lambda r: -r["score"]):
            wk = f"{r['wk_left']:.0f}%" if r["wk_left"] is not None else "?"
            fh = f"{r['fh_left']:.0f}%" if r["fh_left"] is not None else "-"
            rate = f"{r['rate']:.2f}" if r["rate"] is not None else t("status.unknown")
            st = r["blocked"] or t("status.ok")
            extra = t("sep.semicolon").join(r["notes"])
            if fam == "claude" and SCARCE:
                fr = find_row(fable_rows, r["key"])
                fst = fr["blocked"] or t("status.ok")
                if fst != st:
                    st = t("status.split", model=SCARCE_LABEL, other=st, scarce=fst)
                fb = (r.get("usage") or {}).get(SCARCE)
                left = f"{max(0, 100 - fb['used']):.0f}%" if fb else "?"
                extra = t("sep.semicolon").join(r["notes"] + [t("status.scarce_left", model=SCARCE_LABEL, left=left)])

            if fam == "codex" and (r.get("reset_credits") or 0) > 0:
                extra = t("sep.semicolon").join(filter(None, [extra, t("status.vouchers", count=r["reset_credits"])]))
            dirs = t("sep.plus").join(dir_label(d) for d in r.get("dirs", [r["dir"]]))
            if fam == "codex":
                dirs = dir_label(r["dir"])
            if fam == "agy":
                dirs = "agy"
                partner = (r.get("usage") or {}).get("partner_seven_day")
                if partner:
                    extra = t("sep.semicolon").join(filter(None, [extra, t("status.agy_partner", left=max(0, 100 - partner["used"]))]))
            name = who(r) + (t("status.lead_tag") if r.get("is_lead") else "")
            if not bars:
                print(f"{name:<16}{dirs:<24}{wk:>6}{fmt_clock(r['wk_reset']):>14}{fh:>9}{rate:>10}{r['busy_detail']:>10}  {st}"
                      + (t("paren", text=extra) if extra else ""))
                continue
            # card layout: one bar per line, CJK-aware alignment, fits 80 columns
            label = lambda text: "  " + pad(text, 9)
            print(f"\n{pad(name, 22)}{dirs}")
            print(label(t("status.bar.weekly")) + bar(r["wk_left"]) + "   " + t("status.bar.reset", clock=fmt_clock(r["wk_reset"])))
            if fam in ("claude", "agy"):
                print(label(t("status.bar.five_hour")) + bar(r["fh_left"]))
                if SCARCE:
                    fb = (r.get("usage") or {}).get(SCARCE)
                    print(label(SCARCE_LABEL) + bar(max(0, 100 - fb["used"]) if fb else None))
            card_extra = t("sep.semicolon").join(r["notes"]) if fam == "claude" else extra   # the bar already shows the bucket
            print(label(col("state")) + t("sep.dot").join(filter(None, [st, r["busy_detail"], col("rate") + " " + rate, card_extra])))
        if fam == "claude":
            missing = quiet_call(hookless_accounts, default=[])
            if missing:
                print(t("status.launch_hook_missing", keys=t("sep.list").join(missing)))
        if fam == "codex":
            upd = [(r["fetched"], r.get("live")) for r in rows if r["fetched"]]
            if upd:
                stamp, live = max(upd)
                print(t("status.codex_updated", clock=fmt_clock(stamp),
                        source=t("status.codex_live") if live else t("status.codex_local", hours=CODEX_STALE_H)))

    quiet_call(print_status_events)


def interactive():
    """True when a person is reading the output on a terminal. Separate so tests can replace it."""
    return sys.stdout.isatty()


def hookless_accounts():
    """Claude accounts that may take new sessions (`launch` not false) but have no quota-router hook in their
    settings.json, so `qr launch` passes them over."""
    from . import install, launch
    return [a["key"] for a in CLAUDE_ACCOUNTS if a["key"] not in LAUNCH_OFF
            and install.installed_state(a["dir"])[0] not in launch.HOOKED]


# ---- side records (history, reset events, notices): every failure here is swallowed and never affects scoring ----
HISTORY_LIMIT = 2 * 1024 * 1024
OBSERVATION_JSON_LIMIT = 1024 * 1024
INDEX_TTL = 30 * 86400
RESET_TOLERANCE = 60
OBSERVATIONS = ContextVar("quota_observations", default=None)
EVENT_KINDS = ("scheduled", "early", "refill", "adjusted")


def window_name(name):
    if name == "seven_day":
        return t("window.seven_day")
    return t("window.five_hour") if name == "five_hour" else t("window.scarce", model=SCARCE_LABEL)


def quiet_call(fn, *args, default=None, **kwargs):
    """A side-record failure must not reach the routing flow or count as a routing failure."""
    try:
        return fn(*args, **kwargs)
    except Exception:
        return default


def observe_reading(family, key, reading):
    """The hook collects first and writes after the decision, so recording never delays a query; it is still
    inside the hook's total time budget and is skipped when that is used up."""
    pending = OBSERVATIONS.get()
    if pending is None:
        record_observation(family, key, reading)
    else:
        pending.append((family, key, copy.deepcopy(reading)))


@contextmanager
def collect_observations():
    token = OBSERVATIONS.set([])
    try:
        yield
    finally:
        pending = OBSERVATIONS.get()
        OBSERVATIONS.reset(token)
        if pending and within_budget("history"):
            quiet_call(record_observations, pending)


@contextmanager
def observation_lock():
    """Separate from the cache / lease locks; waits at most 50 ms and skips this record otherwise."""
    with open(os.path.join(STATE, "observations.lock"), "a", encoding="utf-8") as f:
        end = time.monotonic() + min(0.05, remaining())
        while not platform_compat.try_lock(f):
            if time.monotonic() >= end:
                yield False
                return
            time.sleep(0.001)
        try:
            yield True
        finally:
            platform_compat.unlock(f)


def observation_save(name, value):
    """Atomic replace, without using the main flow's error log."""
    path = os.path.join(STATE, name)
    tmp = path + "." + uuid.uuid4().hex + ".tmp"
    try:
        with open(tmp, "x", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        platform_compat.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def observation_lines(name):
    """Bad lines are skipped one by one; reports read the history, pending recovery only scans event ids."""
    result = []
    try:
        with open(os.path.join(STATE, name), encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict):
                        result.append(row)
                except (ValueError, TypeError):
                    pass
    except OSError:
        pass
    return result


def observation_append(name, row):
    """Caller holds the observation lock; when over the size limit the complete lines of the second half are kept."""
    path = os.path.join(STATE, name)
    data = (json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode()
    with open(path, "a+b") as f:
        if f.tell():
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                f.write(b"\n")
        f.write(data)
    size = os.path.getsize(path)
    if size > HISTORY_LIMIT:
        with open(path, "rb") as f:
            f.seek(size // 2)
            f.readline()
            tail = f.read()
        tmp = path + "." + uuid.uuid4().hex + ".tmp"
        try:
            with open(tmp, "xb") as f:
                f.write(tail)
            platform_compat.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def observation_identity(family, reading):
    if family == "claude":
        return ("email:" + reading["email"] if reading.get("email") else
                "token:" + reading["token_sig"] if reading.get("token_sig") else None)
    return "account:" + reading["account_sig"] if reading.get("account_sig") else None


def reset_event(family, identity, key, window, old, new):
    """Classify only live readings that are definitely newer; a small date change counts only together with a clear drop."""
    if new["observed_at"] <= old["observed_at"]:
        return None
    before, after = old["used"], new["used"]
    a, b = old.get("reset"), new.get("reset")
    drop = before - after
    changed = a != b if a is None or b is None else abs(b - a) > RESET_TOLERANCE
    jump = a is not None and b is not None and b - a > (600 if window == "five_hour" else 3600)
    # observation times come from the local clock and scheduled times from the vendor, so "has it arrived" gets some tolerance.
    passed = a is not None and new["observed_at"] >= a - RESET_TOLERANCE   # the new reading is at or past the old scheduled time
    crossed = passed and old["observed_at"] < a                              # the old scheduled time lies between the two observations
    if passed and (drop >= RESET_DROP or (crossed and (jump or b is None))):
        # scheduled rollover. When the baseline itself was read after the scheduled time (crossed is false), quota must visibly have come back,
        # otherwise "old date first, new date on the next reading" would record one rollover twice.
        kind = "scheduled"
    elif changed and a is not None and b is not None and b > a and not passed and drop > 0 and (jump or drop >= RESET_DROP):
        kind = "early"
    elif drop >= RESET_DROP:
        # changes not covered above (a date moving earlier, say) still keep the evidence of an actual refill.
        kind = "refill"
    elif changed and drop <= 0:
        kind = "adjusted"
    else:
        return None
    if window == "five_hour" and kind not in ("early", "refill"):
        return None
    # keep the full signed interval; a negative value means it may be later than scheduled, so "early" cannot be claimed.
    early = [a - new["observed_at"], a - old["observed_at"]] if a is not None else None
    event_id = fingerprint(json.dumps([family, identity, window, old, new], sort_keys=True))
    return {"id": event_id, "created_at": now(), "family": family, "identity": identity, "key": key,
            "window": window, "kind": kind, "before": old, "after": new, "early_seconds": early}


def observation_json(name, default):
    """Small side files are read with a size cap; a corrupt or oversized file is left for the caller to rebuild."""
    try:
        with open(os.path.join(STATE, name), "rb") as f:
            data = f.read(OBSERVATION_JSON_LIMIT + 1)
        return json.loads(data) if len(data) <= OBSERVATION_JSON_LIMIT else None
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        return None


def observation_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def valid_observation(value):
    return (isinstance(value, dict) and observation_number(value.get("observed_at"))
            and observation_number(value.get("used")) and 0 <= value["used"] <= 100
            and (value.get("reset") is None or observation_number(value["reset"])))


def valid_event(event):
    """Validate each event and try rendering it, so one bad record cannot break a whole batch of notices."""
    try:
        if not isinstance(event, dict) or not isinstance(event.get("usage", {}), dict):
            return False
        for win in event.get("usage", {}).values():
            if not isinstance(win, dict) or not observation_number(win.get("used")) or not 0 <= win["used"] <= 100:
                return False
        if "display_name" in event and not isinstance(event["display_name"], str):
            return False
        return (isinstance(event, dict) and isinstance(event.get("id"), str)
                and isinstance(event.get("key"), str) and event.get("family") in config.FAMILIES
                and event.get("kind") in EVENT_KINDS and event.get("window") in windows()
                and observation_number(event.get("created_at"))
                and valid_observation(event.get("before")) and valid_observation(event.get("after"))
                and bool(notice_text(event)) and bool(event_line(event)))
    except (KeyError, TypeError, ValueError, OverflowError, OSError, AttributeError):
        return False


def observation_index():
    raw = observation_json("observation-index.json", {})
    index = {}
    for key, line in (raw.items() if isinstance(raw, dict) else ()):
        if not isinstance(line, dict):
            continue
        live = line.get("live")
        live = {w: v for w, v in live.items() if w in windows() and valid_observation(v)} if isinstance(live, dict) else {}
        history = line.get("history")
        if not isinstance(history, dict) or not observation_number(history.get("observed_at")) or not isinstance(history.get("usage"), dict):
            history = None
        pending = line.get("pending")
        pending = [e for e in pending if valid_event(e)] if isinstance(pending, list) else []
        latest = max([v["observed_at"] for v in live.values()] + [history["observed_at"] if history else 0])
        if now() - latest >= INDEX_TTL and not pending:
            continue
        index[key] = {"live": live}
        if history:
            index[key]["history"] = history
        if pending:
            index[key]["pending"] = pending
    return index


def flush_observation_events(index):
    """Baseline and pending events are committed together; events and notices are then filled in idempotently by event id before pending is cleared."""
    pending = [e for line in index.values() for e in line.get("pending", [])]
    if not pending:
        return
    ids = {e["id"] for e in observation_lines("reset-events.jsonl") if isinstance(e.get("id"), str)}
    for event in pending:
        if event["id"] not in ids:
            observation_append("reset-events.jsonl", event)
            ids.add(event["id"])
    store_notices(pending)
    for line in index.values():
        line.pop("pending", None)
    observation_save("observation-index.json", index)


def record_observation(family, key, reading):
    record_observations([(family, key, reading)])


def record_observations(readings):
    """One lock and one index read/write per query; pending work from an interrupted run is recovered first."""
    with observation_lock() as locked:
        if not locked:
            return
        index = observation_index()
        flush_observation_events(index)
        for family, key, reading in readings:
            index_observation(index, family, key, reading)
        observation_save("observation-index.json", index)
        flush_observation_events(index)


def index_observation(index, family, key, reading):
    """Caller holds the lock; directories of the same account share the history and the per-window live baseline."""
    identity = observation_identity(family, reading)
    stamp = fetched(reading)
    if not identity or not stamp:
        return
    usage = {}
    for name in windows():
        win = (reading.get("usage") or {}).get(name)
        if not win:
            continue
        used = win.get("used")
        reset = win.get("reset")
        if not isinstance(used, (int, float)) or not math.isfinite(used) or not 0 <= used <= 100:
            continue
        if reset is not None and (not isinstance(reset, (int, float)) or not math.isfinite(reset)):
            reset = None
        usage[name] = {"used": used, "reset": reset}
    if not usage:
        return
    row = {"observed_at": stamp, "collected_at": now(), "family": family, "identity": identity,
           "key": key, "source": "live" if reading.get("live") else "local", "usage": usage}
    if family == "codex":
        row.update(credits=reading.get("credits"), reset_credits=reading.get("reset_credits", 0))
    line = index.setdefault(family + ":" + identity, {})
    previous = line.get("history")
    baseline = line.setdefault("live", {})
    events = []
    if row["source"] == "live":
        for window, win in usage.items():
            new = dict(win, observed_at=stamp)
            old = baseline.get(window)
            if not old or stamp > old["observed_at"]:
                if old:
                    event = reset_event(family, identity, key, window, old, new)
                    if event:
                        event["usage"] = usage
                        event["display_name"] = who({"key": key, "email": reading.get("email") if family == "claude" else None})
                        events.append(event)
                baseline[window] = new
    newer = not previous or stamp > previous["observed_at"]
    if row["source"] == "live" or newer:
        same = previous and previous.get("usage") == row["usage"]
        if not same or stamp - previous["observed_at"] >= 600:
            observation_append("usage-history.jsonl", row)
            if newer:
                line["history"] = row
    if events:
        line.setdefault("pending", []).extend(events)


def store_notices(events):
    """Caller holds the observation lock; delivery records expire together with the notice."""
    notices = usable_notices(observation_json("reset-notices.json", []))
    ids = {n["id"] for n in notices}
    for event in events:
        if (valid_event(event) and notifiable(event) and event["id"] not in ids
                and now() - event["created_at"] < NOTICE_TTL):
            notices.append(dict(event, seen=[]))
            ids.add(event["id"])
    observation_save("reset-notices.json", notices)


def notifiable(event):
    """The lead is only told when quota actually came back (usage dropped by RESET_DROP points or more); date changes and rollovers of unused windows are just recorded."""
    return event["kind"] != "adjusted" and event["before"]["used"] - event["after"]["used"] >= RESET_DROP


def usable_notices(raw):
    notices = []
    for notice in raw if isinstance(raw, list) else []:
        if not valid_event(notice) or not notifiable(notice) or now() - notice["created_at"] >= NOTICE_TTL:
            continue
        seen = notice.get("seen")
        notices.append(dict(notice, seen=[s for s in seen if isinstance(s, str)] if isinstance(seen, list) else []))
    return notices


def observation_clock(ts):
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def early_text(event):
    interval = event.get("early_seconds")
    if interval is None:
        return t("early.unknown")
    low, high = interval
    scale, unit = (86400, t("unit.days")) if max(abs(low), abs(high)) >= 86400 else (3600, t("unit.hours"))
    if high < 0:
        return t("early.late", low=-high / scale, high=-low / scale, unit=unit)
    if low < 0:
        return t("early.at_most", high=high / scale, unit=unit)
    return t("early.early", low=low / scale, high=high / scale, unit=unit)


def event_timing(event):
    """Each kind describes itself; a date change or a normal rollover is never called "early"."""
    a, b = event["before"].get("reset"), event["after"].get("reset")
    clock = lambda ts: observation_clock(ts) if ts is not None else t("clock.unknown")
    if event["kind"] == "scheduled":
        return t("timing.scheduled", clock=clock(a))
    if event["kind"] == "adjusted":
        return t("timing.adjusted", old=clock(a), new=clock(b))
    if event["kind"] == "refill":
        if a is not None and b is not None and abs(b - a) > RESET_TOLERANCE:
            return t("timing.refill_moved", old=clock(a), new=clock(b))
        if a is None or b is None:
            return t("timing.refill_unknown")
        left = a - event["after"]["observed_at"]
        return t("timing.refill_same_left", days=left / 86400) if left > 0 else t("timing.refill_same_passed")
    return early_text(event)


def window_label(event):
    return t("window.codex_weekly") if event["family"] == "codex" and event["window"] == "seven_day" else window_name(event["window"])


def notice_text(event):
    before, after = event["before"], event["after"]
    text = t("notice.reset" if after["used"] <= 5 else "notice.refill",
             name=event.get("display_name") or event["key"], window=window_label(event), timing=event_timing(event),
             before=100 - before["used"], after=100 - after["used"],
             start=observation_clock(before["observed_at"]), end=observation_clock(after["observed_at"]))
    usage = event.get("usage") or {}
    for name in windows():
        if name == event["window"] or (event["family"] == "codex" and name == SCARCE):
            continue
        win = usage.get(name)
        if not win:
            continue  # this account has no such window (Codex has no five-hour window, say) or it was not read this time
        label = t("window.codex_weekly") if event["family"] == "codex" and name == "seven_day" else window_name(name)
        text += t("notice.other_window", window=label, left=100 - win["used"])
    if event["family"] == "codex":
        text += t("notice.tail_codex")
    elif event["window"] == SCARCE:
        text += t("notice.tail_scarce", model=SCARCE_LABEL)
    elif event["window"] == "seven_day":
        text += t("notice.tail_weekly", model=SCARCE_LABEL) if SCARCE else t("notice.tail_weekly_plain")
    else:
        text += t("notice.tail_five_hour", model=SCARCE_LABEL) if SCARCE else t("notice.tail_five_hour_plain")
    return text


def recent_notices(session=None, limit=None):
    """The CLI does not consume notices; the hook marks each as seen per session (or per lead / working-directory fingerprint)."""
    with observation_lock() as locked:
        if not locked:
            return []
        # even when this call hits a hot cache, events and notices of an already committed baseline are filled in.
        quiet_call(flush_observation_events, observation_index())
        all_notices = observation_json("reset-notices.json", [])
        notices = usable_notices(all_notices)
        sid = fingerprint(session) if session is not None else None
        pending = [n for n in notices if n["created_at"] <= now() and (not sid or sid not in n["seen"])]
        pending.sort(key=lambda n: n["created_at"], reverse=True)
        selected = pending if limit is None else pending[:limit]
        texts = [notice_text(n) for n in selected]
        if limit is not None and len(pending) > limit:
            texts.append(t("notice.more", count=len(pending) - limit))
        if sid:
            # the overflow has not been delivered; it is kept for the next dispatch.
            for notice in selected:
                notice.setdefault("seen", []).append(sid)
        if (sid and selected) or notices != all_notices:
            observation_save("reset-notices.json", notices)
        return texts


def expiring_hint(orows):
    candidates = []
    for row in orows:
        if (row.get("blocked") or row.get("is_lead") or row.get("wk_left") is None
                or row["wk_left"] < 30 or row.get("rate") is None or not fetched(row)):
            continue
        for name in weekly_windows(row["family"]):
            win = (row.get("usage") or {}).get(name)
            left = min(row["wk_left"], 100 - win["used"]) if win else 0
            if win and win.get("reset") and 0 < win["reset"] - now() <= 86400 and left >= 30:
                candidates.append((win["reset"], row, name, left))
    if not candidates:
        return None
    reset, row, name, left = min(candidates, key=lambda item: item[0])
    label = t("hint.expiring_scarce_label", model=SCARCE_LABEL) if name == SCARCE else ""
    task = t("hint.expiring_task_scarce", model=SCARCE_LABEL) if name == SCARCE else t("hint.expiring_task")
    return t("hint.expiring", who=who(row), label=label, hours=(reset - now()) / 3600, left=left, task=task)


def event_line(event, scheduled_note=False):
    before, after = event["before"], event["after"]
    text = t("event.line", start=observation_clock(before["observed_at"]), end=observation_clock(after["observed_at"]),
             name=event.get("display_name") or event["key"], window=window_label(event),
             kind=t("event." + event["kind"]), before=before["used"], after=after["used"], timing=event_timing(event))
    if scheduled_note and event["kind"] == "scheduled":
        hours = max(0, before["reset"] - before["observed_at"]) / 3600
        text += t("event.scheduled_note", left=100 - before["used"], hours=hours)
    return text


def recent_events(seconds, limit=None):
    events = []
    for event in observation_lines("reset-events.jsonl"):
        try:
            if 0 <= now() - event["after"]["observed_at"] <= seconds:
                event_line(event)
                events.append(event)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            pass
    events.sort(key=lambda e: e["after"]["observed_at"])
    return events[-limit:] if limit else events


def print_status_events():
    for text in recent_notices():
        print(text)
    events = recent_events(86400, 5)
    if events:
        print("\n" + t("status.recent_events"))
        for event in events:
            print(event_line(event))


DENY_CODES = ("credits", "overage", "lead", "five_hour", "weekly", "no_context", "none", "other")


def decision_code(message):
    """Reduce a deny message, in any language, to one of DENY_CODES.
    Only the refusal itself is looked at; the credit policy and window names at the end are not the reason."""
    body = min((message.split(marker, 1)[0] for marker in i18n.every("marker.earliest_reset")), key=len)
    for name in DENY_CODES[:6]:
        if re.search("|".join(i18n.every("re." + name)), body):
            return name
    return "none" if any(text in body for text in i18n.every("block.none")) else "other"


def decision_reason(message):
    """Reason category of one deny record in decisions.log, in the display language.
    Current records hold "blocked: <English category>"; records written before the log became English-only
    hold the full message in whatever language was active, and are classified from its wording."""
    m = re.match(r"blocked: ([^;|]+)", message)
    if m:
        code = next((name for name in DENY_CODES if i18n.EN["deny." + name] == m[1].strip()), "other")
    else:
        code = decision_code(message)
    return t("deny." + code)


def decision_statistics(days, rows=()):
    """The log has no year; it is inferred as the most recent matching month and day not after now."""
    from collections import Counter
    accounts, reasons = {who(row): Counter() for row in rows}, Counter()
    # records hold the account key (hashed when it has unusual characters); it is shown under the account's name.
    names = {log_token(key): who(row) for row in rows for key in row.get("keys", [row["key"]])}
    blocked = allowed = failed = unknown = 0
    current = datetime.fromtimestamp(now())
    # the separator between account and directory in "pick=account (dir)", in every language.
    who_separator = "|".join(re.escape(text.split("{who}", 1)[1].split("{dir}", 1)[0]) for text in i18n.every("log.who"))
    try:
        with open(os.path.join(STATE, "decisions.log"), encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    parts = line.rstrip().split(" | ")
                    stamp = datetime.strptime(f"{current.year}-{parts[0]}", "%Y-%m-%d %H:%M:%S")
                    if stamp.timestamp() > now():
                        stamp = stamp.replace(year=current.year - 1)
                    if now() - stamp.timestamp() > days * 86400:
                        continue
                    mode = parts[5]
                    message = parts[7] if len(parts) > 7 else ""
                    failed += mode == "fail"
                    allowed += any(text in message for text in i18n.every("marker.credits_allowed"))  # English now; older records may be translated
                    if mode == "deny":
                        blocked += 1
                        # reduce to a fixed reason so times and account names do not split one reason into many.
                        reason = decision_reason(message)
                        reasons[reason] += 1
                    category = {"auto": "auto", "pick": "auto", "explicit": "explicit", "check": "explicit", "resume": "resume"}.get(mode)
                    if category:
                        account = re.split(who_separator, parts[6].split("=", 1)[-1], maxsplit=1)[0]
                        account = names.get(account, account)
                        accounts.setdefault(account, Counter())[category] += 1
                    elif mode in ("resume-unknown", "explicit-unknown"):
                        unknown += 1
                except (ValueError, IndexError, OverflowError):
                    continue
    except OSError:
        pass
    for account, counts in sorted(accounts.items()):
        print(t("stats.account", account=account, auto=counts["auto"], explicit=counts["explicit"], resume=counts["resume"]))
    top = t("sep.list").join(t("stats.reason", reason=k, count=v) for k, v in reasons.most_common(3))
    print(t("stats.blocked", count=blocked, reasons=top or t("stats.none")))
    print(t("stats.totals", allowed=allowed, failed=failed, unknown=unknown))


def report(days=7):
    """Display only, no reservation; uses the normal query cooldown and time budget."""
    rows = []
    with budget(time.monotonic() + HOOK_BUDGET_SEC):
        for family in managed_families():
            rows.extend(dict(row, family=family) for row in read_family(family))
    print(t("report.title", days=days))
    events = recent_events(days * 86400)
    for title, scheduled in ((t("report.scheduled"), True), (t("report.other"), False)):
        print(title)
        group = [e for e in events if (e["kind"] == "scheduled") == scheduled]
        for event in group:
            print(event_line(event, scheduled_note=True))
        if not group:
            print(t("report.empty"))
    print("\n" + t("report.pace"))
    for row in rows:
        for name in weekly_windows(row["family"]):
            label = t("window.codex_weekly") if row["family"] == "codex" else window_name(name)
            win = (row.get("usage") or {}).get(name)
            hours = (now() - (win["reset"] - 7 * 86400)) / 3600 if win and win.get("reset") else 0
            if not reading_fresh(row, row["family"]) or not 24 <= hours <= 168 or win["reset"] <= now():
                text = t("report.no_data")
            else:
                projected = win["used"] / hours * 168
                left = 100 - min(100, projected)
                text = t("report.pace_line", elapsed=hours / 168 * 100, used=win["used"], left=left)
                if projected >= 100:
                    days_left = (100 - win["used"]) / (win["used"] / hours) / 24 if win["used"] > 0 else 0
                    text += t("report.runs_out", days=days_left)
            print(t("report.row", who=who(row), window=label, text=text))
    print(t("report.disclaimer"))
    print("\n" + t("report.stats"))
    decision_statistics(days, rows)


def watch_status(force, interval, bars=True):
    """`qr status --watch [N]`: the status table again every N seconds until Ctrl-C. On a terminal the screen is
    cleared before each redraw; elsewhere (a pipe, a file) the tables are separated by a line of dashes. --refresh
    applies to the first draw only: later draws reuse the cached numbers, so watching does not hammer the usage
    endpoints."""
    import io
    from contextlib import redirect_stdout
    tty = sys.stdout.isatty()
    try:
        while True:
            buf = io.StringIO()
            with redirect_stdout(buf):
                status(force, bars=bars)
            force = False
            if tty:
                if platform_compat.system() == "windows":
                    os.system("cls")
                else:
                    sys.stdout.write("\x1b[2J\x1b[H")
            else:
                print("-" * 72)
            print(t("watch.header", seconds=interval, clock=time.strftime("%H:%M:%S")))
            sys.stdout.write(buf.getvalue())
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        print()


def main(argv):
    force = "--refresh" in argv
    model = None
    if "--model" in argv:
        i = argv.index("--model") + 1
        if i >= len(argv) or argv[i].startswith("--"):
            print(t("usage.model"), file=sys.stderr)
            raise SystemExit(2)
        model = argv[i]
    for arg in argv:
        if arg.startswith("--model="):
            model = arg.split("=", 1)[1]
    if not argv or argv[0] in ("-h", "--help"):
        print(t("help"))
    elif argv[0] == "pick" and len(argv) > 1 and argv[1] in config.FAMILIES:
        pick(argv[1], peek="--peek" in argv, env_only="--env" in argv, force=force, model=model)
    elif argv[0] == "check" and len(argv) > 2 and argv[1] in config.FAMILIES:
        check_account(argv[1], argv[2], model=model, peek="--peek" in argv, force=force)
    elif argv[0] == "watch" or argv[0] == "status" and len(argv) > 1 and argv[1] in ("watch", "bars"):
        # `qr watch [N]`, `qr status watch [N]` and `qr status bars`: the same as --watch / --bars, without the dashes
        main([argv[0] if argv[0] == "status" else "status", "--" + (argv[1] if argv[0] == "status" else "watch"),
              *(argv[2:] if argv[0] == "status" else argv[1:])])
    elif argv[0] == "status":
        if "--watch" in argv:
            i = argv.index("--watch") + 1
            interval = 10
            if i < len(argv) and not argv[i].startswith("--"):
                try:
                    interval = int(argv[i])
                except ValueError:
                    interval = 0
            if interval <= 0:
                print(t("usage.watch"), file=sys.stderr)
                raise SystemExit(2)
            watch_status(force, interval)
        else:
            status(force, bars="--bars" in argv)
    elif argv[0] == "report":
        try:
            days = int(argv[argv.index("--days") + 1]) if "--days" in argv else 7
            if days <= 0:
                raise ValueError()
        except (ValueError, IndexError):
            print(t("usage.report"), file=sys.stderr)
            raise SystemExit(2)
        report(days)
    elif argv[0] == "decisions":
        n = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 30
        try:
            with open(os.path.join(STATE, "decisions.log"), encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except FileNotFoundError:
            lines = []
        print("\n".join(lines[-n:]) if lines else t("decisions.empty"))
    elif argv[0] == "leases":
        if "--session" in argv:
            index = argv.index("--session") + 1
            if index >= len(argv) or argv[index].startswith("--"):
                print(t("usage.leases"))
                sys.exit(2)
        if "--clear" in argv or "--session" in argv:
            with state_lock("leases"):
                if "--session" in argv:
                    session = argv[argv.index("--session") + 1]
                    save("leases.json", [l for l in active_leases() if l.get("session", "") != session])
                else:
                    save("leases.json", [])
        for l in active_leases():
            print(l["key"], fmt_clock(l["at"]))
    else:
        print(t("help"))
        sys.exit(2)
