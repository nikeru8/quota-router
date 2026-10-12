"""`qr launch`: start claude on the Claude account with the most quota left. The `claude` launcher that
`qr install` writes runs this for every `claude` typed in a terminal.

The account is chosen once, when the session starts: CLAUDE_CONFIG_DIR is set (or removed) and the real claude
takes over the process. Subagents and workflows that run inside the session stay on that account.

Rules:
- Already decided elsewhere → claude runs unchanged, nothing printed: CLAUDE_CONFIG_DIR is set (by you, or by
  the hook for a dispatch), or this is a command run inside a Claude Code session (CLAUDECODE), whose
  dispatches the hook handles.
- Not an interactive session → unchanged: --help / --version / --bg, a subcommand (`claude mcp …`; any bare
  lower-case word in first position, so an unknown subcommand is never mistaken for a prompt), and `-p` unless
  `[launch] route_print = true` (scripts and `qr pick` write the default account as `env -u CLAUDE_CONFIG_DIR`).
- Resumed conversation (--resume / -r / --continue / -c / --from-pr) → the account that holds it.
- Otherwise → the best Claude account by the hook's scoring, among the accounts that are set up for sessions
  (`launch = true` and a quota-router hook in their settings.json). There is no lead yet, so no account is
  protected as the lead's; an account whose quota can be read comes before one whose quota cannot.
- Never refuses: when nothing fits, nothing can be read or this module fails, claude starts on the default
  account, with one line on stderr (when stderr is a terminal) saying why.
"""
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time

from . import config, core as qr, hook, platform_compat
from .i18n import t

MARKER = "quota-router launcher"      # in the launcher file; a `claude` on PATH that carries it is never started
KEEP = object()                        # leave CLAUDE_CONFIG_DIR as it is
NO_LEAD = ""                           # read_family(lead_key=NO_LEAD): no row is the lead's (no key is empty)
GRACE_SEC = 2                          # past the budget by this much, the choice is abandoned for the default
RESOLVED = "quota-router-launch 1"     # first line of `qr launch --resolve`, which the sh launcher reads
PASS_FLAGS = {"--help", "-h", "--version", "-v",
              "--bg"}                  # background sessions are kept per config directory; `claude agents` looks in the default one
NOT_SESSION = hook.CLAUDE_SUBCOMMANDS | {"migrate-installer"}
SUBCOMMAND_WORD = re.compile(r"[a-z][a-z0-9-]*")
# Options of claude that take a value, synced from `claude --help` of claude 2.1.296. The value must not be read
# as a subcommand (`claude --effort high` is a session). This is the only list; tests/test_launch.py parses every
# entry. --model, --resume / -r and --from-pr are read apart because their value matters.
VALUE_FLAGS = frozenset({
    "--add-dir", "--agent", "--agents", "--allowedTools", "--allowed-tools", "--append-system-prompt",
    "--append-system-prompt-file", "--autocompact", "--betas", "--debug-file", "--disallowedTools",
    "--disallowed-tools", "--effort", "--fallback-model", "--file", "--input-format", "--json-schema",
    "--max-budget-usd", "--max-turns", "--mcp-config", "-n", "--name", "--output-format", "--permission-mode",
    "--permission-prompt-tool", "--permission-prompts", "--plugin-dir", "--plugin-url", "--session-id",
    "--setting-sources", "--settings", "--system-prompt", "--system-prompt-file", "--tools", "-w", "--worktree"})
HOOKED = ("installed", "outdated", "manual", "foreign")   # installed_state() values with a hook that Bash reaches
LAUNCHES = "launched.json"
LAUNCHES_KEPT_SEC = 30 * 86400
LAUNCHES_KEPT = 500


# ---- the real claude ------------------------------------------------------------
def is_launcher(path):
    """Whether a file is a quota-router launcher (this one or one written for another Python or data directory)."""
    try:
        with open(path, "rb") as f:
            return MARKER.encode() in f.read(512)
    except OSError:
        return False


def real_claude(path=None, skip=None):
    """Absolute path of the claude the launcher stands in for: the first `claude` on PATH that is neither in the
    launcher's directory nor a launcher itself. Empty and relative PATH entries (the working directory) are not
    searched. None when there is none. Looked up on every start, so an update or a move of claude is followed."""
    path = os.environ.get("PATH", "") if path is None else path
    skip = platform_compat.canonical(skip or platform_compat.default_launcher_dir())
    for directory in path.split(os.pathsep):
        directory = directory.strip('"')
        if not directory or not os.path.isabs(directory) or platform_compat.canonical(directory) == skip:
            continue
        for name in platform_compat.executable_names("claude"):
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK) and not is_launcher(candidate):
                return candidate
    return None


# ---- what kind of start this is ---------------------------------------------------
def parse(args):
    """None when claude should run untouched (help, version, --bg, a subcommand); otherwise
    {"resume": None | {"id": …} | {"continue": True}, "model": the --model value or None, "print": -p given}.

    `--resume` without an id opens claude's picker, which lists the conversations of one account, and
    `--from-pr` resumes a conversation this tool cannot map to a file: both go where --continue would."""
    resume, model, printing, first = None, None, False, None
    i = 0
    while i < len(args):
        word = args[i]
        if word == "--":
            break                                       # the rest is the prompt
        if word in PASS_FLAGS:
            return None
        if word in ("--resume", "-r"):
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                resume = {"id": args[i + 1]}
                i += 2
                continue
            resume = resume or {"continue": True}
        elif word.startswith("--resume="):
            value = word.split("=", 1)[1]
            resume = {"id": value} if value else resume or {"continue": True}
        elif word in ("--continue", "-c"):
            resume = resume or {"continue": True}
        elif word == "--from-pr" or word.startswith("--from-pr="):
            resume = resume or {"continue": True}
            i += 2 if word == "--from-pr" else 1
            continue
        elif word == "--model" and i + 1 < len(args):
            model = args[i + 1]
            i += 2
            continue
        elif word.startswith("--model="):
            model = word.split("=", 1)[1] or None
        elif word in ("-p", "--print") or word.startswith("--print="):
            printing = True                             # what follows print is the prompt, not a subcommand
        elif re.fullmatch(r"-[A-Za-z]{2,}", word):      # short options written together: -pc
            printing = printing or "p" in word
            if "c" in word:
                resume = resume or {"continue": True}
        elif word.startswith("-"):
            i += 2 if word in VALUE_FLAGS else 1
            continue
        elif first is None:
            first = word
            if not printing and (word in NOT_SESSION or SUBCOMMAND_WORD.fullmatch(word)):
                return None
        i += 1
    return {"resume": resume, "model": model, "print": printing}


# ---- which accounts may take a session ---------------------------------------------
NOT_READY = {"off": "launch.not_enabled", "no-hook": "launch.no_hook"}


def readiness(cfg):
    """{Claude account key: None when a new session may start there, else "off" (`launch = false`) or "no-hook"}.
    An account is set up for sessions when `launch` is true and its settings.json has a quota-router hook. Each
    account directory carries its own settings (hook, permissions, MCP servers, model, CLAUDE.md); quota-router
    does not copy them between directories, and a directory without the hook would not route its own dispatches."""
    from . import install
    out = {}
    for account in cfg.family("claude"):
        if not account["launch"]:
            out[account["key"]] = "off"
        elif install.installed_state(account["dir"])[0] not in HOOKED:
            out[account["key"]] = "no-hook"
        else:
            out[account["key"]] = None
    return out


# ---- where sessions were started (for --continue) ------------------------------------
def place(cwd):
    """A working directory as it is stored: a hash, never the path."""
    return hashlib.sha256(platform_compat.canonical(cwd).encode("utf-8", "replace")).hexdigest()[:16]


def remember_launch(cwd, key):
    """One record per session started through the launcher (or with CLAUDE_CONFIG_DIR named): the hashed working
    directory, the account key and the time. Claude's session id is not known yet when the session starts."""
    if not key:
        return
    with qr.state_lock("launched"):
        kept = [x for x in qr.load(LAUNCHES, []) if isinstance(x, dict) and
                isinstance(x.get("at"), (int, float)) and qr.now() - x["at"] < LAUNCHES_KEPT_SEC]
        kept.append({"place": place(cwd), "key": key, "at": qr.now()})
        qr.save(LAUNCHES, kept[-LAUNCHES_KEPT:])


def dispatched(path):
    """Whether a conversation file was written by a one-shot or SDK run (`claude -p`, the hook's dispatches) or a
    subagent: its first records carry `entrypoint` "sdk-…" or `isSidechain` true. Unknown counts as interactive."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for _ in range(8):
                text = f.readline(256 * 1024)
                if not text:
                    break
                try:
                    record = json.loads(text)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("isSidechain") is True:
                    return True
                entry = record.get("entrypoint")
                if isinstance(entry, str):
                    return entry.startswith("sdk")
    except OSError:
        pass
    return False


def continue_account(cwd):
    """The account `claude --continue` should open in this directory.

    1. The most recent launch here whose account has a conversation of this directory: that is the session the
       user last started here; a dispatch the hook sent elsewhere in the meantime is not.
    2. Otherwise the newest conversation file of this directory across the accounts, leaving out files written by
       one-shot runs or subagents (when the file says so) and files newer than a launch here on another account
       (most likely dispatches of that session). None when nothing is found."""
    here = place(cwd)
    launches = sorted((x for x in qr.load(LAUNCHES, []) if isinstance(x, dict) and x.get("place") == here
                       and isinstance(x.get("key"), str) and isinstance(x.get("at"), (int, float))),
                      key=lambda x: -x["at"])
    files = {key: hook.bounded_files(os.path.join(directory, "projects", hook.slug(cwd)), "*.jsonl")
             for key, directory in hook.account_dirs("claude").items()}
    for launch in launches:
        if files.get(launch["key"]):
            return launch["key"]
    best = None
    for key, paths in files.items():
        others = [x["at"] for x in launches if x["key"] != key]
        for path in paths:
            if qr.remaining() <= 0:
                return None
            try:
                stamp = os.path.getmtime(path)
            except OSError:
                continue
            if (others and stamp > max(others)) or (best and stamp <= best[0]) or dispatched(path):
                continue
            best = (stamp, key)
    return best[1] if best else None


# ---- choosing the account ---------------------------------------------------------
def line(*parts):
    """The one stderr line: the parts that are not empty, joined, never more than one line."""
    return " ".join(part.replace("\n", " ") for part in parts if part)


def overage(row):
    """A loud warning when the session would be billed as paid overage on this account; "" otherwise."""
    if not row or not row.get("paid"):
        return ""
    if row.get("will_spend"):
        return t("launch.overage_now", who=qr.who(row))
    return t("launch.overage_risk", who=qr.who(row)) if row.get("pay_risk") else ""


def launch_rows(raw, launchable):
    """The rows a new session may land on, each represented by its first directory that is set up for sessions
    (two directories logged in to one account are one row; the other directory may not be)."""
    out = []
    for row in raw:
        allowed = [(key, directory) for key, directory in zip(row["keys"], row["dirs"]) if key in launchable]
        if allowed:
            out.append(dict(row, key=allowed[0][0], dir=allowed[0][1]))
    return out


def add_sessions(raw):
    """Interactive sessions running on an account count as jobs for a new session: three terminals opened one
    after the other should not all land on one account. (The hook does not count them.)"""
    sessions = qr.running_sessions()
    for row in raw:
        row["jobs"] += sum(sessions.get(key, 0) for key in row["keys"])
    return raw


def record(mode, row=None, text="", model=None, cmd="", account=None):
    if qr.within_budget("decision-log"):
        qr.log_decision("qr launch", "claude", mode, row, text=text, cmd=cmd, lead="-",
                        model=qr.model_class(model), account=account)


def default_row(raw, model):
    """The default account's row, scored, to warn about paid overage when the session falls back to it."""
    key = qr.DEFAULT_KEY["claude"]
    return qr.find_row(qr.score_rows("claude", raw, model), key) if key else None


def to_default(cwd, text, mode_text, model, cmd):
    record("launch-default", text=mode_text, model=model, cmd=cmd, account=qr.DEFAULT_KEY["claude"])
    qr.quiet_call(remember_launch, cwd, qr.DEFAULT_KEY["claude"])
    return None, text


def resumed(parsed, cwd, cmd):
    model = parsed["model"]
    if parsed["resume"].get("id"):
        key = hook.resume_account("claude", parsed["resume"], cwd)
    else:
        key = continue_account(cwd)
    if key is None:
        return to_default(cwd, t("launch.resume_unknown"), "conversation location unknown", model, cmd)
    raw = add_sessions(qr.read_family("claude", lead_key=NO_LEAD))
    # like the hook: the shared quota decides whether the account can go on; the model (the scarce one when
    # none is written) decides whether it would be billed.
    row = qr.find_row(qr.score_rows("claude", raw, model or "other"), key)
    paying = qr.find_row(qr.score_rows("claude", raw, model), key)
    qr.add_lease(key)
    gone = hook.out_of_quota(row)
    text = line(t("launch.resume", reason=qr.reason(row)),
                t("launch.out_of_quota", who=qr.who(row), reason=gone) if gone else "", overage(paying))
    record("launch-resume", row, "kept the original account", cmd=cmd)
    qr.quiet_call(remember_launch, cwd, key)
    # the conversation is in this directory, whichever directory represents the account in the rows.
    return next(a["dir"] for a in qr.CLAUDE_ACCOUNTS if a["key"] == key), text


def new_session(parsed, ready, cwd, cmd):
    model = parsed["model"]
    raw = add_sessions(qr.read_family("claude", lead_key=NO_LEAD))
    candidates = launch_rows(raw, {key for key, why in ready.items() if why is None})
    if not candidates:
        reasons = t("sep.semicolon").join(t("launch.blocked_row", key=key, reason=t(NOT_READY[why])) for key, why in ready.items())
        return to_default(cwd, line(t("launch.none_ready", reasons=reasons), overage(default_row(raw, model))),
                          "no account is set up for sessions", model, cmd)
    candidates = qr.retry_blocked("claude", candidates, model)
    rows = qr.score_rows("claude", candidates, model)
    readable = any(r.get("usage") for r in rows)
    with qr.state_lock("leases"):
        leases = qr.active_leases()
        qr.refresh_busy(rows, leases)
        # For a dispatch the hook ranks an account whose quota cannot be read in the middle of the others. A
        # session runs for hours: an account that can be read and has room comes first.
        known = [r for r in rows if r["rate"] is not None]
        best, warn, _ = qr.choose("claude", known if any(not r["blocked"] for r in known) else rows)
        if best is not None and readable:
            leases.append({"key": best["key"], "at": qr.now(), "session": ""})
            qr.save("leases.json", leases)
    if best is None:
        reasons = t("sep.semicolon").join(t("launch.blocked_row", key=r["key"], reason=r["blocked"] or t("block.no_reading"))
                                          for r in rows)
        return to_default(cwd, line(t("launch.blocked", reasons=reasons), overage(default_row(raw, model))),
                          "no account can take it", model, cmd)
    if not readable:
        # when no account can be read at all, ranking them would be guesswork: the default account is used.
        return to_default(cwd, line(t("launch.unreadable"), overage(default_row(raw, model))),
                          "no account could be read", model, cmd)
    paid = overage(best)
    record("launch", best, "on paid overage" if paid else "launched with a warning" if warn else "", cmd=cmd)
    qr.quiet_call(remember_launch, cwd, best["key"])
    return best["dir"], line(t("launch.routed", reason=qr.reason(best)), warn, paid)


def decide(parsed, cfg, cwd, cmd, deadline):
    """(directory or None, stderr line) for a session that is not passed through."""
    with qr.budget(deadline):
        with qr.collect_observations():
            if parsed["resume"]:
                return resumed(parsed, cwd, cmd)
            return new_session(parsed, readiness(cfg), cwd, cmd)


def remember_named(cwd):
    """A session started with CLAUDE_CONFIG_DIR written out is not routed, but it is recorded for --continue."""
    cfg = config.load()
    if cfg.family("claude"):
        qr.configure(cfg)
        remember_launch(cwd, qr.dir_key(os.environ["CLAUDE_CONFIG_DIR"]))


def choose(args, cwd=None):
    """(directory, line). directory: KEEP leaves CLAUDE_CONFIG_DIR as it is, None removes it, a path sets it.
    line: the one stderr line, or None. Never raises: every failure ends on the default account."""
    if os.environ.get("CLAUDECODE"):
        return KEEP, None
    parsed = parse(args)
    if parsed is None:
        return KEEP, None
    cwd = cwd or os.getcwd()
    if "CLAUDE_CONFIG_DIR" in os.environ:
        if not parsed["print"]:
            qr.quiet_call(remember_named, cwd)
        return KEEP, None
    try:
        cfg = config.load()
        if not cfg.family("claude") or parsed["print"] and not cfg["launch.route_print"]:
            return KEEP, None                           # not managed, or a one-shot run left as written
        qr.configure(cfg)
    except config.ConfigMissing as e:
        return None, t("launch.no_config", path=e.path)
    except config.ConfigError as e:
        return None, t("launch.config_error", path=e.path, detail=e.detail)
    except Exception as e:
        return None, t("launch.failed", error=type(e).__name__)
    cmd = shlex.join(["claude", *args])
    budget = qr.HOOK_BUDGET_SEC
    deadline = time.monotonic() + budget
    result = {}

    def work():
        try:
            result["choice"] = decide(parsed, cfg, cwd, cmd, deadline)
        except Exception as e:
            result["error"] = e

    # A file system or lock that stops answering cannot be interrupted cooperatively; the worker is abandoned
    # instead, and claude takes over the process.
    worker = threading.Thread(target=work, name="qr-launch", daemon=True)
    worker.start()
    worker.join(budget + GRACE_SEC)
    if worker.is_alive():
        qr.quiet_call(qr.log_error, "launch-timeout")
        return None, t("launch.timeout", seconds=budget)
    if "error" in result:
        error = result["error"]
        qr.quiet_call(qr.log_error, "launch", error)
        qr.quiet_call(qr.log_decision, "qr launch", "claude", "fail", text=type(error).__name__, cmd=cmd, lead="-")
        return None, t("launch.failed", error=type(error).__name__)
    return result["choice"]


# ---- starting claude --------------------------------------------------------------
def environment(directory):
    env = dict(os.environ)
    if directory is None:
        env.pop("CLAUDE_CONFIG_DIR", None)
    elif directory is not KEEP:
        env["CLAUDE_CONFIG_DIR"] = directory
    return env


def start(program, args, directory):
    """Replace this process with claude (POSIX), so the terminal and its signals belong to claude. Windows has no
    exec: claude runs as a child, Ctrl+C is left to it, and its exit status becomes this one's."""
    argv = [program, *args]
    env = environment(directory)
    if platform_compat.system() == "windows":
        child = subprocess.Popen(argv, env=env)
        while True:
            try:
                raise SystemExit(child.wait())
            except KeyboardInterrupt:
                continue
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execve(program, argv, env)
    except OSError as error:
        print(f"quota-router: cannot start {program}: {error.strerror or type(error).__name__}", file=sys.stderr)
        raise SystemExit(126)


def resolved(program, directory):
    """What the sh launcher reads from `qr launch --resolve`: a tag line, keep / unset / set, the directory and
    the program. The launcher then starts claude itself, so a Python that cannot run never stands in the way."""
    action = "keep" if directory is KEEP else "unset" if directory is None else "set"
    values = [RESOLVED, action, directory if action == "set" else "", program]
    if any("\n" in value or "\r" in value for value in values):
        raise SystemExit(1)                             # not representable line by line: the launcher falls back
    sys.stdout.write("\n".join(values) + "\n")
    sys.stdout.flush()


def stderr_is_terminal():
    try:
        return sys.stderr.isatty()
    except Exception:
        return False


def main(argv):
    resolve = argv[:1] == ["--resolve"]
    argv = argv[1:] if resolve else argv
    args = argv[1:] if argv[:1] == ["--"] else list(argv)
    program = real_claude()
    if program is None:
        if resolve:
            raise SystemExit(1)                         # the launcher looks on its own and reports it
        print(t("launch.not_found", dir=platform_compat.default_launcher_dir()), file=sys.stderr)
        raise SystemExit(127)
    try:
        directory, text = choose(args)
    except KeyboardInterrupt:
        raise SystemExit(130)
    if text and stderr_is_terminal():
        # only for a person: a script reading `claude … 2>&1` must not get an extra line.
        try:
            print(text, file=sys.stderr, flush=True)
        except Exception:
            pass                                        # a closed or odd stderr must not keep claude from starting
    if resolve:
        return resolved(program, directory)
    start(program, args, directory)
