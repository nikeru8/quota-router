"""`qr launch`: start claude on the Claude account with the most quota left. The `claude` launcher that
`qr install` writes runs this for every `claude` typed in a terminal.

The account is chosen once, when the session starts: CLAUDE_CONFIG_DIR is set (or removed) and the real claude
replaces this process. Subagents and workflows that run inside the session stay on that account.

Rules:
- Already decided elsewhere → claude runs unchanged, nothing printed: CLAUDE_CONFIG_DIR is set (by you, or by
  the hook for a dispatch), or this is a command run inside a Claude Code session (CLAUDECODE), whose
  dispatches the hook handles. The hook writes the default account as `env -u CLAUDE_CONFIG_DIR`, which only
  CLAUDECODE tells apart from a terminal.
- Not a session → unchanged: --help / --version, and a subcommand (`claude mcp …`; any bare lower-case word
  in first position, so an unknown subcommand is never mistaken for a prompt).
- Resumed conversation (--resume / -r / --continue / -c) → the account that holds it.
- Otherwise → the best Claude account by the hook's scoring. There is no lead yet, so no account is protected
  as the lead's; accounts with `launch = false` are never chosen.
- Never refuses: when nothing fits, nothing can be read or this module fails, claude starts on the default
  account, with one line on stderr saying why.
"""
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
PASS_FLAGS = {"--help", "-h", "--version", "-v"}
NOT_SESSION = hook.CLAUDE_SUBCOMMANDS | {"migrate-installer"}
SUBCOMMAND_WORD = re.compile(r"[a-z][a-z0-9-]*")
# options whose value is the next word; it must not be read as a subcommand. --resume / -r / --model are parsed apart.
VALUE_FLAGS = (hook.CLAUDE_VALUE_FLAGS - {"--resume", "-r", "--model"}) | {
    "--agent", "--agents", "--fallback-model", "--add-dir", "--plugin-dir", "--setting-sources", "--tools",
    "--betas", "--max-budget-usd", "--json-schema", "--max-turns", "--permission-prompt-tool"}


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
    """None when claude should run untouched (help, version, a subcommand); otherwise
    {"resume": None | {"id": …} | {"continue": True}, "model": the --model value or None}.

    `--resume` without an id opens claude's picker, which lists the conversations of one account: it is opened
    on the account of the most recent conversation in this directory, as for --continue."""
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
        elif word == "--model" and i + 1 < len(args):
            model = args[i + 1]
            i += 2
            continue
        elif word.startswith("--model="):
            model = word.split("=", 1)[1] or None
        elif word in ("-p", "--print") or word.startswith("--print="):
            printing = True                             # what follows print is the prompt, not a subcommand
        elif word.startswith("-"):
            i += 2 if word in VALUE_FLAGS else 1
            continue
        elif first is None:
            first = word
            if not printing and (word in NOT_SESSION or SUBCOMMAND_WORD.fullmatch(word)):
                return None
        i += 1
    return {"resume": resume, "model": model}


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
    """The rows a new session may land on, each represented by its first directory that has launch enabled
    (two directories logged in to one account are one row; the other directory may have launch = false)."""
    out = []
    for row in raw:
        allowed = [(key, directory) for key, directory in zip(row["keys"], row["dirs"]) if key in launchable]
        if allowed:
            out.append(dict(row, key=allowed[0][0], dir=allowed[0][1]))
    return out


def record(mode, row=None, text="", model=None, cmd="", account=None):
    if qr.within_budget("decision-log"):
        qr.log_decision("qr launch", "claude", mode, row, text=text, cmd=cmd, lead="-",
                        model=qr.model_class(model), account=account)


def default_row(raw, model):
    """The default account's row, scored, to warn about paid overage when the session falls back to it."""
    key = qr.DEFAULT_KEY["claude"]
    return qr.find_row(qr.score_rows("claude", raw, model), key) if key else None


def resumed(parsed, cwd, cmd):
    model = parsed["model"]
    key = hook.resume_account("claude", parsed["resume"], cwd)
    if key is None:
        record("launch-default", text="conversation location unknown", model=model, cmd=cmd,
               account=qr.DEFAULT_KEY["claude"])
        return None, t("launch.resume_unknown")
    raw = qr.read_family("claude", lead_key=NO_LEAD)
    # like the hook: the shared quota decides whether the account can go on; the model (the scarce one when
    # none is written) decides whether it would be billed.
    row = qr.find_row(qr.score_rows("claude", raw, model or "other"), key)
    paying = qr.find_row(qr.score_rows("claude", raw, model), key)
    qr.add_lease(key)
    gone = hook.out_of_quota(row)
    text = line(t("launch.resume", reason=qr.reason(row)),
                t("launch.out_of_quota", who=qr.who(row), reason=gone) if gone else "", overage(paying))
    record("launch-resume", row, "kept the original account", cmd=cmd)
    # the conversation is in this directory, whichever directory represents the account in the rows.
    return next(a["dir"] for a in qr.CLAUDE_ACCOUNTS if a["key"] == key), text


def new_session(parsed, launchable, cmd):
    model = parsed["model"]
    raw = qr.read_family("claude", lead_key=NO_LEAD)
    candidates = launch_rows(raw, launchable)
    if not candidates:
        record("launch-default", text="no account has launch enabled", model=model, cmd=cmd,
               account=qr.DEFAULT_KEY["claude"])
        return None, line(t("launch.none_enabled"), overage(default_row(raw, model)))
    candidates = qr.retry_blocked("claude", candidates, model)
    rows = qr.score_rows("claude", candidates, model)
    # an account whose quota cannot be read can take a dispatch in the hook, next to accounts that can be read;
    # when none can, ranking them would be guesswork and the default account is used instead.
    readable = any(r.get("usage") for r in rows)
    with qr.state_lock("leases"):
        leases = qr.active_leases()
        qr.refresh_busy(rows, leases)
        best, warn, _ = qr.choose("claude", rows)
        if best is not None and readable:
            leases.append({"key": best["key"], "at": qr.now(), "session": ""})
            qr.save("leases.json", leases)
    if best is None:
        record("launch-default", text="no account can take it", model=model, cmd=cmd, account=qr.DEFAULT_KEY["claude"])
        reasons = t("sep.semicolon").join(t("launch.blocked_row", key=r["key"], reason=r["blocked"] or t("block.no_reading"))
                                          for r in rows)
        return None, line(t("launch.blocked", reasons=reasons), overage(default_row(raw, model)))
    if not readable:
        record("launch-default", text="no account could be read", model=model, cmd=cmd, account=qr.DEFAULT_KEY["claude"])
        return None, line(t("launch.unreadable"), overage(default_row(raw, model)))
    paid = overage(best)
    record("launch", best, "on paid overage" if paid else "launched with a warning" if warn else "", cmd=cmd)
    return best["dir"], line(t("launch.routed", reason=qr.reason(best)), warn, paid)


def decide(parsed, cfg, cwd, cmd, deadline):
    """(directory or None, stderr line) for a session that is not passed through."""
    launchable = {a["key"] for a in cfg.family("claude") if a["launch"]}
    with qr.budget(deadline):
        with qr.collect_observations():
            if parsed["resume"]:
                return resumed(parsed, cwd, cmd)
            return new_session(parsed, launchable, cmd)


def choose(args, cwd=None):
    """(directory, line). directory: KEEP leaves CLAUDE_CONFIG_DIR as it is, None removes it, a path sets it.
    line: the one stderr line, or None. Never raises: every failure ends on the default account."""
    if "CLAUDE_CONFIG_DIR" in os.environ or os.environ.get("CLAUDECODE"):
        return KEEP, None
    parsed = parse(args)
    if parsed is None:
        return KEEP, None
    try:
        cfg = config.load()
        if not cfg.family("claude"):
            return KEEP, None                           # a family with no accounts is not managed
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
            result["choice"] = decide(parsed, cfg, cwd or os.getcwd(), cmd, deadline)
        except Exception as e:
            result["error"] = e

    # A file system or lock that stops answering cannot be interrupted cooperatively; the worker is abandoned
    # instead, and claude replaces this whole process with it.
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


def main(argv):
    args = argv[1:] if argv[:1] == ["--"] else list(argv)
    program = real_claude()
    if program is None:
        print(t("launch.not_found", dir=platform_compat.default_launcher_dir()), file=sys.stderr)
        raise SystemExit(127)
    try:
        directory, text = choose(args)
    except KeyboardInterrupt:
        raise SystemExit(130)
    if text:
        try:
            print(text, file=sys.stderr, flush=True)
        except Exception:
            pass                                        # a closed or odd stderr must not keep claude from starting
    start(program, args, directory)
