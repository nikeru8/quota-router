"""PreToolUse (Bash) hook: a dispatch (claude -p / codex exec) that names no account gets the roomiest one.

Rules:
- No account named → insert the account qr picks and add a one-line explanation for the lead.
- The lead named an account → use it as written; warn and suggest when that account should not be used.
- Resumed conversation (claude --resume/-r/--continue/-c, codex exec resume) → stay on the account that
  holds the conversation; if it is out of quota, block and ask the lead to dispatch again elsewhere
  with a summary of the previous round.
- The tool itself fails → let the original command through with a "routing failed" note.
"""
import glob, json, os, re, sys, time, shlex, threading

from . import config, core as qr, platform_compat
from .i18n import t


def account_dirs(fam):
    """{account key: directory on disk}; an account at the default location maps to the CLI's default directory."""
    return {a["key"]: a["dir"] or config.default_dir(fam) for a in qr.accounts(fam)}


# ---- mask: quoted text and heredoc bodies become spaces; the length stays the same ----
# `<<` or `<<-`, then the delimiter: any shell word. It may start with a digit and hold `-`, `.` and the like
# (`<<123`, `<<END-TEXT`), be quoted (`<<'END OF TEXT'`) or escaped (`<<\EOF`).
HEREDOC = re.compile(r"""<<(-?)[ \t]*(?:'([^'\n]*)'|"([^"\n]*)"|((?:\\.|[^\s;&|<>()'"`\\])+))""")


def mask(cmd):
    out = list(cmd)
    i, n = 0, len(cmd)
    pending_heredocs = []
    arithmetic = 0
    while i < n:
        c = cmd[i]
        if c == "\\" and i + 1 < n:
            if cmd[i + 1] == "\n":
                out[i] = " "
            out[i + 1] = " "; i += 2; continue
        if c == "'":
            j = cmd.find("'", i + 1)
            j = n - 1 if j < 0 else j
            for k in range(i + 1, j): out[k] = " "
            i = j + 1; continue
        if c == '"':
            j = i + 1
            while j < n and cmd[j] != '"':
                j += 2 if cmd[j] == "\\" else 1
            j = min(j, n - 1)
            for k in range(i + 1, j): out[k] = " "
            i = j + 1; continue
        if c == "#" and (i == 0 or cmd[i - 1] in " \t\n;&|("):
            j = cmd.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j): out[k] = " "
            i = j; continue
        # inside $(( … )) and (( … )) a `<<` is a shift, not a heredoc.
        if c == "(" and cmd.startswith("((", i):
            arithmetic += 1
            i += 2; continue
        if c == ")" and arithmetic and cmd.startswith("))", i):
            arithmetic -= 1
            i += 2; continue
        if c == "<" and cmd.startswith("<<<", i):
            i += 3; continue  # a here-string: the word after it is an ordinary word.
        m = HEREDOC.match(cmd, i) if c == "<" and not arithmetic else None
        if m:
            delimiter = next(g for g in m.groups()[1:] if g is not None)
            if m.group(4) is not None:
                delimiter = re.sub(r"\\(.)", r"\1", delimiter)
            pending_heredocs.append((delimiter, m.group(1) == "-"))
            i = m.end(); continue
        if c == "\n" and pending_heredocs:
            j = i + 1
            for delim, dash in pending_heredocs:
                while j < n:
                    e = cmd.find("\n", j)
                    e = n if e < 0 else e
                    line = cmd[j:e]
                    done = (line.lstrip("\t") if dash else line) == delim
                    if not done:
                        for k in range(j, e): out[k] = " "
                    j = e + 1
                    if done:
                        break
            pending_heredocs = []
            i = j; continue
        i += 1
    return "".join(out)


SEP = re.compile(r"&&|\|\||[;&|\n()`]|\$\(")


def segments(masked):
    """[(start, end)] of every simple command in the string."""
    pos, segs = 0, []
    for m in SEP.finditer(masked):
        segs.append((pos, m.start())); pos = m.end()
    segs.append((pos, len(masked)))
    return segs


def tokens(masked, s, e):
    return [(m.start() + s, m.group()) for m in re.finditer(r"(?:[^\s'\"]+|'[^']*'|\"[^\"]*\")+", masked[s:e])]


ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
VALUE_FLAGS = {"-m", "--model", "-c", "--config", "-s", "--sandbox", "-C", "--cd",
               "-p", "--profile", "-a", "--ask-for-approval", "-i", "--image", "--add-dir",
               "--output-schema", "-o", "--output-last-message", "--color",
               "--enable", "--disable", "--local-provider", "--remote", "--remote-auth-token-env"}
CLAUDE_VALUE_FLAGS = {"--model", "--mcp-config", "--settings", "--permission-mode",
                      "--output-format", "--input-format", "--system-prompt", "--append-system-prompt",
                      "--resume", "-r", "--session-id", "--allowedTools", "--disallowedTools"}
PREFIX_OPTIONS = {
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
    "nohup": set(), "time": {"-o", "--output", "-f", "--format"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"}, "gtimeout": {"-s", "--signal", "-k", "--kill-after"},
    "noglob": set(), "command": set(), "exec": {"-a"},
    "sudo": {"-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt", "-C", "--close-from", "-T", "--command-timeout", "-R", "--chroot", "-D", "--chdir", "-r", "--role", "-t", "--type"},
    "nice": {"-n", "--adjustment"}, "caffeinate": {"-t", "-w"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"}, "setsid": set(),
    "xargs": {"-E", "-I", "-J", "-L", "-n", "-P", "-s", "-d", "--delimiter", "--max-args", "--max-procs", "--arg-file", "-a"},
    "parallel": {"-j", "--jobs", "-S", "--sshlogin", "--joblog", "--results", "--colsep", "--delay", "--timeout", "--retries", "-a", "--arg-file", "--max-args", "-N", "-n", "--halt", "--wd", "--workdir"},
}
KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{"}
CLAUDE_SUBCOMMANDS = {"mcp", "config", "update", "doctor", "plugin", "install", "auth", "setup-token", "daemon", "agents"}


def command_position(prev, raw_prev=None):
    """Accept known launcher prefixes only (PREFIX_OPTIONS: env, timeout, nice, nohup, …), whose own `--` ends their
    options. After any other program, an interpreter included (`python3 -c … -- claude -p hi`, `git log -- claude`),
    what follows is that program's arguments: whether it runs them cannot be told from the text."""
    if prev and prev[-1] in ("-exec", "-execdir") and any(
            os.path.basename(x) == "find" and command_position(prev[:i]) for i, x in enumerate(prev[:-1])):
        return True
    i = 0
    while i < len(prev):
        word = prev[i]
        if ASSIGN.match(word) or word in KEYWORDS:
            i += 1
            continue
        base = os.path.basename(word)
        # this exact alarm wrapper occurs in real use; only the precise program is accepted, not perl arguments in general.
        if base == "perl" and raw_prev and i + 3 == len(prev) and prev[i + 1] == "-e":
            return bool(re.fullmatch(r"alarm\s+\d+;\s*exec\s+@ARGV", raw_prev[i + 2]))
        if base not in PREFIX_OPTIONS:
            return False
        i += 1
        while i < len(prev) and prev[i].startswith("-"):
            option = prev[i]
            if base == "command" and option in ("-v", "-V"):
                return False
            i += 1
            if option == "--":
                break
            if option in PREFIX_OPTIONS[base]:
                if i >= len(prev):
                    return False
                i += 1
        if base in ("timeout", "gtimeout"):
            if i >= len(prev) or not re.fullmatch(r"[0-9.]+[smhd]?|\$[A-Za-z_][A-Za-z0-9_]*|\$\{[^}]+\}|[\"'][ ]*[\"']", prev[i]):
                return False
            i += 1
        if base == "env":
            while i < len(prev) and raw_prev and raw_prev[i].startswith(("$(", "`")):
                i += 1
            while i < len(prev) and re.fullmatch(r"\$[A-Za-z_][A-Za-z0-9_]*|\$\{[A-Za-z_][A-Za-z0-9_]*\}", prev[i]):
                i += 1
    return True


PARSE_GRACE_SEC = 0.25


def parse_limit():
    """When reading the command text has to be finished: the hook's deadline, or a quarter of a second from now
    when that has already passed (a decision from cached readings is still wanted then)."""
    return time.monotonic() + max(qr.remaining(), PARSE_GRACE_SEC)


def check_parse(limit):
    """A command too long to read in time is not guessed at: the hook reports a routing failure and lets it run as written."""
    if time.monotonic() > limit:
        raise qr.BudgetTimeout("parse")


def cli_name(word):
    """`claude`, `/opt/bin/claude`, `claude.exe`, `C:/Users/me/bin/codex.cmd` → `claude` / `codex`; anything else as it is."""
    name = os.path.basename(word)
    stem, dot, suffix = name.rpartition(".")
    return stem.lower() if dot and suffix.lower() in ("exe", "cmd") and stem.lower() in ("claude", "codex") else name


def find_dispatches(cmd):
    """[{fam, pos (insertion point), seg, resume: {...} | None}]."""
    limit = parse_limit()
    mk = mask(cmd)
    found = []
    scopes = [opaque_substitutions(mk), mk]
    for scan, s, e in [(scan, s, e) for scan in dict.fromkeys(scopes) for s, e in segments(scan)]:
        toks = tokens(scan, s, e)
        words, raws = [word for _, word in toks], None
        for idx, (p, t) in enumerate(toks):
            base = cli_name(t)
            if base not in ("claude", "codex"):
                continue
            check_parse(limit)
            if raws is None:
                raws = [raw_token(cmd, tok) for tok in toks]
            if not command_position(words[:idx], raws[:idx]):
                continue
            rest = words[idx + 1:]
            if any(x in ("--help", "-h", "--version", "-V") for x in rest):
                continue
            if base == "claude":
                k = 0
                while k < len(rest) and rest[k].startswith("-"):
                    if rest[k] in ("-p", "--print") or rest[k].startswith("--print="):
                        break  # what follows print is the prompt, not a management subcommand.
                    k += 2 if rest[k] in CLAUDE_VALUE_FLAGS else 1
                if k < len(rest) and rest[k] in CLAUDE_SUBCOMMANDS:
                    continue
            if base == "claude" and any(x in ("-p", "--print") or x.startswith("--print=") for x in rest):
                res = None
                for k, x in enumerate(rest):
                    if x in ("--resume", "-r") and k + 1 < len(rest) and not rest[k + 1].startswith("-"):
                        res = {"id": raw_token(cmd, toks[idx + 1 + k + 1])}
                    elif x in ("--continue", "-c"):
                        res = {"continue": True}
                found.append({"fam": "claude", "pos": p, "seg": (s, e), "resume": res})
                break
            k = 0
            while k < len(rest) and rest[k].startswith("-"):
                k += 2 if rest[k] in VALUE_FLAGS else 1
            if base == "codex" and k < len(rest) and rest[k] in ("exec", "e"):
                res = None
                if k + 1 < len(rest) and rest[k + 1] == "resume":
                    after = [x for x in rest[k + 2:] if not x.startswith("-") or x == "--last"]
                    if "--last" in rest[k + 2:]:
                        res = {"last": True}
                    elif after:
                        res = {"id": raw_token(cmd, toks[idx + 1 + k + 2 + rest[k + 2:].index(after[0])])}
                found.append({"fam": "codex", "pos": p, "seg": (s, e), "resume": res})
                break
    unique = {}
    for dispatch in found:
        unique.setdefault(dispatch["pos"], dispatch)
    return sorted(unique.values(), key=lambda d: d["pos"])


def raw_token(cmd, tok):
    p, t = tok
    return cmd[p:p + len(t)].strip("'\"")


def opaque_substitutions(masked):
    """A closed command substitution is kept as one opaque word; it is neither run nor guessed at."""
    out = list(masked)
    i = 0
    while i < len(masked):
        if masked.startswith("$(", i):
            depth, end = 1, i + 2
            while end < len(masked) and depth:
                if masked[end] == "(":
                    depth += 1
                elif masked[end] == ")":
                    depth -= 1
                end += 1
            if depth:
                i += 2
                continue
        elif masked[i] == "`":
            end = masked.find("`", i + 1) + 1
            if not end:
                i += 1
                continue
        else:
            i += 1
            continue
        out[i:end] = "x" * (end - i)
        i = end
    return "".join(out)


def visible_scope(masked, pos):
    """Assignments in a subshell that has ended do not affect what follows; the length is kept so values can be read from the original."""
    out = list(masked[:pos])
    stack = []
    for i, ch in enumerate(out):
        if ch == "(":
            stack.append(i)
        elif ch == ")" and stack:
            start = stack.pop()
            out[start:i + 1] = " " * (i + 1 - start)
    return "".join(out)


CLEARED = ("cleared", None)            # the variable was removed by `unset`
UNSET_BY_ENV = ("unset by env", None)  # the variable was removed by `env -u VAR` in front of the dispatch itself


def explicit_account(cmd, fam, pos, seg, cleared=False):
    """Find where an effective assignment sits in the masked text, then read the quoted value from the original.
    cleared=True reports a variable that was removed last as CLEARED / UNSET_BY_ENV instead of as the default account."""
    var = "CLAUDE_CONFIG_DIR" if fam == "claude" else "CODEX_HOME"
    before = visible_scope(opaque_substitutions(mask(cmd)), pos)
    default = (qr.DEFAULT_KEY[fam] or "?", t("label.main_account") if fam == "claude" else "~/.codex")
    events = []
    exported_state = False
    for start, end in segments(before):
        ts = tokens(before, start, end)
        words = [t for _, t in ts]
        # export / unset must be the command of this segment, not an argument of echo or the like.
        first = 0
        while first < len(words) and words[first] in KEYWORDS:
            first += 1
        exported = first < len(words) and (words[first] == "export" or
                    words[first] in ("declare", "typeset") and "-x" in words[first + 1:])
        if exported and var in words[first + 1:]:
            exported_state = True
            prior = events[-1] if events else None
            if prior and prior[2] is not None:
                events.append((start, "active", prior[2]))
        if first < len(words) and words[first] == "unset" and var in words[first + 1:]:
            events.append((start, "default", None))
            exported_state = False
        for i, (p, word) in enumerate(ts):
            if not word.startswith(var + "="):
                continue
            prefix = end == len(before) and command_position(words[:i])
            standalone = all(ASSIGN.match(x) for x in words)
            if not (exported or prefix or standalone):
                continue
            m = re.match(r"[^=]+=(\"[^\"]*\"|'[^']*'|[^\s;&|()]+)", cmd[p:])
            val = m[1].strip("'\"") if m else ""
            if "$(" in cmd[p:p + len(word)] or "`" in cmd[p:p + len(word)]:
                val = cmd[p + len(var) + 1:p + len(word)]
            if exported:
                exported_state = True
            events.append((p, "active" if exported_state or prefix else "ineffective", val))
        if end == len(before):
            for i, word in enumerate(words):
                if os.path.basename(word) == "env" and command_position(words[:i]):
                    for j in range(i + 1, len(words)):
                        if (words[j] == "-u" and j + 1 < len(words) and words[j + 1] == var or
                                words[j] in ("-u" + var, "--unset=" + var)):
                            events.append((ts[j][0], "default-env", None))
                        raw = cmd[ts[j][0]:ts[j][0] + len(words[j])]
                        if raw.startswith(("$(", "`")):
                            events.append((ts[j][0], "active", raw))
    if not events:
        return None
    _, kind, val = max(events, key=lambda x: x[0])
    if kind in ("default", "default-env"):
        if cleared:
            return UNSET_BY_ENV if kind == "default-env" else CLEARED
        return default
    if kind == "ineffective":
        return "ineffective", val
    real = platform_compat.canonical(val.replace("$HOME", qr.HOME))
    # a value that names the default directory means the account configured with that directory written out
    # (macOS: its own keychain item) when there is one, else the account at the default location
    pairs = sorted(account_dirs(fam).items(), key=lambda kv: kv[0] == qr.DEFAULT_KEY[fam])
    for k, d in pairs:
        if platform_compat.canonical(d) == real:
            return k, val
    return "?", val


def policy_word(cmd, tok, home=False):
    """Join the quoted pieces of one whole word; no shell expansion is performed, and only HOME is accepted in paths."""
    p, word = tok
    raw = cmd[p:p + len(word)]
    if home:
        # single quotes and an escaped $ do not expand; an unknown variable or a command substitution must not be guessed into a pool path.
        parts = re.split(r"('(?:[^']*)'|\\.)", raw)
        # the result is read as shell text below, where a backslash is an escape character: a Windows home
        # directory is written with forward slashes, as the shell itself spells it.
        home_text = qr.HOME.replace("\\", "/") if platform_compat.system() == "windows" else qr.HOME
        for i in range(0, len(parts), 2):
            parts[i] = re.sub(r"\$\{HOME\}|\$HOME(?![A-Za-z0-9_])", lambda _: home_text, parts[i])
        raw = "".join(parts)
        if "$" in raw or "`" in raw:
            return None
    try:
        words = shlex.split(raw)
        return words[0] if len(words) == 1 else None
    except ValueError:
        return None


def policy_setting(cmd, pos, var, home=False):
    """Same masking and effective-prefix positions as explicit_account(); the policy layer reads whole words and leaves that parser alone."""
    before = visible_scope(opaque_substitutions(mask(cmd)), pos)
    events = []
    exported_state = False
    control_depth = 0
    for start, end in segments(before):
        ts = tokens(before, start, end)
        words = [t for _, t in ts]
        values = [policy_word(cmd, t, home) for t in ts]
        if words and words[0] in ("fi", "done", "esac"):
            control_depth = max(0, control_depth - 1)
        if words and words[0] in ("if", "for", "while", "until", "case", "select"):
            control_depth += 1
        uncertain = (control_depth > 0 or bool(re.search(r"(?:&&|\|\||\|)\s*$", before[:start])) or
                     before[end:end + 1] in ("|", "&") and before[end:end + 2] not in ("||", "&&"))
        # export / unset must be the command of this segment, not an argument of echo or the like.
        first = 0
        while first < len(words) and words[first] in KEYWORDS:
            first += 1
        exported = first < len(words) and (words[first] == "export" or
                    words[first] in ("declare", "typeset") and "-x" in words[first + 1:])
        if exported and var in values[first + 1:]:
            exported_state = exported_state or not uncertain
            prior = events[-1] if events else None
            if prior and prior[2] is not None:
                events.append((start, "active", None if uncertain else prior[2]))
        if first < len(words) and words[first] == "unset" and var in values[first + 1:]:
            events.append((start, "default", None))
            exported_state = False
        for i, (p, word) in enumerate(ts):
            if not word.startswith(var + "="):
                continue
            prefix = end == len(before) and command_position(words[:i])
            standalone = all(ASSIGN.match(x) for x in words)
            if not (exported or prefix or standalone):
                continue
            val = values[i].split("=", 1)[1] if values[i] is not None else None
            if uncertain and not prefix:
                val = None
            if exported:
                exported_state = exported_state or not uncertain
            events.append((p, "active" if exported_state or prefix else "ineffective", val))
        if end == len(before):
            for i, word in enumerate(words):
                if os.path.basename(word) == "env" and command_position(words[:i]):
                    for j in range(i + 1, len(words)):
                        if (values[j] in ("-i", "--ignore-environment", "-") or
                                values[j] in ("-u", "--unset") and j + 1 < len(words) and values[j + 1] == var or
                                values[j] in ("-u" + var, "--unset=" + var)):
                            events.append((ts[j][0], "default", None))
                        raw = cmd[ts[j][0]:ts[j][0] + len(words[j])]
                        if raw.startswith(("$(", "`")):
                            events.append((ts[j][0], "active", raw))
    if not events:
        return None
    _, kind, val = max(events, key=lambda x: x[0])
    return kind, val


def credits_allowed(cmd, pos, seg):
    """Consent counts only as a prefix of this very dispatch: `QR_ALLOW_CREDITS=1 codex exec …` or `env QR_ALLOW_CREDITS=1 codex exec …`.
    export, a standalone assignment, another segment or the hook's own environment never count. Whether an export is in
    effect depends on conditionals, pipelines, braces and subshells, and review kept finding ways around the check;
    where money is involved, not recognising it is the safer mistake."""
    scan = opaque_substitutions(mask(cmd))
    ts = tokens(scan, seg[0], pos)
    words = [w for _, w in ts]
    if not command_position(words, [raw_token(cmd, t) for t in ts]):
        return False
    allowed = False
    for i, tok in enumerate(ts):
        word = tok[1]
        value = policy_word(cmd, tok)
        if word.startswith("QR_ALLOW_CREDITS="):
            # the whole word must be exactly 1; variables, command substitutions or concatenations do not count.
            allowed = value == "QR_ALLOW_CREDITS=1"
        elif value in ("-i", "--ignore-environment", "-", "-uQR_ALLOW_CREDITS", "--unset=QR_ALLOW_CREDITS"):
            allowed = False
        elif value in ("-u", "--unset") and i + 1 < len(ts) and policy_word(cmd, ts[i + 1]) == "QR_ALLOW_CREDITS":
            allowed = False
    return allowed


def dispatch_model(cmd, dispatch):
    """Option positions come from the masked words, values from the original; the prompt and heredocs are not options."""
    if dispatch["fam"] == "codex":
        return "other", True
    scan = opaque_substitutions(mask(cmd))
    ts = tokens(scan, dispatch["pos"], dispatch["seg"][1])
    value, written = None, False
    i = 1
    while i < len(ts):
        _, word = ts[i]
        if word == "--":
            break
        if word == "--model" and i + 1 < len(ts):
            value, written = policy_word(cmd, ts[i + 1]), True
            i += 2
            continue
        if word.startswith("--model="):
            value = (policy_word(cmd, ts[i]) or "--model=").split("=", 1)[1]
            written = True
        elif word in CLAUDE_VALUE_FLAGS:
            i += 2
            continue
        i += 1
    # a value that cannot be determined (variable, command substitution, empty, odd concatenation) is scored as the scarce model: conservative, and its paid-overage check is not skipped.
    dynamic = written and (value is None or value == "" or "$" in value or "`" in value)
    dispatch["model_dynamic"] = dynamic
    if dispatch["resume"] and not written:
        # a resume without a model keeps its account as long as the shared window has room (the conversation may
        # not be on the scarce model at all), but whether it would cost money is judged as for any dispatch
        # without a model: against the scarce model's bucket too.
        dispatch["pay_model"] = qr.model_class(None)
        return "other", written
    return (qr.model_class(None) if dynamic else qr.model_class(value)), written


def resolved_explicit(cmd, dispatch):
    """Keep explicit_account()'s result; the policy layer additionally recognises fully static concatenated paths.
    When the variable was cleared last (`unset`, `env -u VAR`, `env -i`), no account is named: None, and the
    dispatch is routed like any other. The one exception is `env -u CLAUDE_CONFIG_DIR claude …`: that is how the
    Claude account at the default location is spelled (`qr pick claude` prints it, and so does this hook), so it
    names that account."""
    var = "CLAUDE_CONFIG_DIR" if dispatch["fam"] == "claude" else "CODEX_HOME"
    setting = policy_setting(cmd, dispatch["pos"], var, home=True)
    ex = explicit_account(cmd, dispatch["fam"], dispatch["pos"], dispatch["seg"], cleared=True)
    if ex == UNSET_BY_ENV and dispatch["fam"] == "claude":
        return qr.DEFAULT_KEY["claude"] or "?", t("label.main_account")
    if ex in (CLEARED, UNSET_BY_ENV) or setting and setting[0] == "default":
        return None
    if ex and ex[0] != "ineffective":
        val = setting[1] if setting and setting[0] == "active" else None
        key = qr.account_key(dispatch["fam"], val) if val is not None else None
        if key:
            ex = key, val
    return ex


# ---- resumed conversations: which account holds the record ------------------------
def slug(path):
    """Claude Code's name for a project directory: every character that is not a letter or digit becomes `-`
    (`/Users/me/app` → `-Users-me-app`, `C:\\Users\\me\\app` → `C--Users-me-app`)."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def bounded_files(base, pattern, recursive=False):
    """Files under `base` whose name matches `pattern` (one path component, or `*/name` for one level down);
    recursive=True looks at every depth. Empty as soon as the time budget is used up. `base` is taken literally."""
    import fnmatch
    if qr.remaining() <= 0:
        return []
    out = []
    if recursive:
        for directory, dirs, files in os.walk(base, followlinks=True):
            if qr.remaining() <= 0:
                return []
            dirs[:] = [name for name in dirs if not name.startswith(".")]
            out.extend(os.path.join(directory, name) for name in files if not name.startswith(".") and fnmatch.fnmatch(name, pattern))
    else:
        for path in glob.iglob(os.path.join(glob.escape(base), *pattern.split("/"))):
            if qr.remaining() <= 0:
                return []
            out.append(path)
    return out


def newest_mtime(files):
    newest = None
    for path in files:
        if qr.remaining() <= 0:
            return None
        try:
            stamp = os.path.getmtime(path)
        except OSError:
            continue
        newest = stamp if newest is None else max(newest, stamp)
    return newest


def resume_account(fam, res, cwd):
    session = glob.escape(res["id"]) if res.get("id") else None
    if fam == "claude":
        if session:
            for k, d in account_dirs("claude").items():
                if bounded_files(os.path.join(d, "projects"), f"*/{session}.jsonl"):
                    return k
            return None
        best = None
        for k, d in account_dirs("claude").items():
            fs = bounded_files(os.path.join(d, "projects", slug(cwd)), "*.jsonl")
            if fs:
                t = newest_mtime(fs)
                if t is not None and (not best or t > best[0]):
                    best = (t, k)
        return best[1] if best and qr.remaining() > 0 else None
    if session:
        for k, d in account_dirs("codex").items():
            if bounded_files(os.path.join(d, "sessions"), f"*{session}*.jsonl", recursive=True):
                return k
        return None
    best = None
    for k, d in account_dirs("codex").items():
        fs = bounded_files(os.path.join(d, "sessions"), "*.jsonl", recursive=True)
        if fs:
            t = newest_mtime(fs)
            if t is not None and (not best or t > best[0]):
                best = (t, k)
    return best[1] if best and qr.remaining() > 0 else None


def dir_prefix(fam, directory):
    """The environment prefix inserted before a dispatch. The directory comes from the config file, so it is quoted as
    one shell word (forward slashes on Windows, which Git Bash accepts): a space or a `;` in it stays part of the path."""
    if fam == "claude" and directory is None:
        return "env -u CLAUDE_CONFIG_DIR "
    return f"env {config.ENV_DIR[fam]}={platform_compat.shell_quote(directory)} "


def env_prefix(fam, key):
    return dir_prefix(fam, None if fam == "claude" and key == qr.DEFAULT_KEY["claude"] else account_dirs(fam)[key])


def row_prefix(fam, row):
    """Representative directory of a (possibly merged) row (row['dir']) → the environment prefix inserted before the command."""
    return dir_prefix(fam, row["dir"])


def out_of_quota(row):
    if row["wk_left"] is not None and row["wk_left"] <= 0:
        return t("blocked.scarce_out", model=qr.SCARCE_LABEL) if row.get("wk_source", "shared") != "shared" else t("blocked.weekly_out")
    if row["fh_left"] is not None and row["fh_left"] < 1:
        return t("blocked.five_hour_out")
    return None


# ---- main flow ----------------------------------------------------------------
def decide(cmd, cwd, lead_key=None, session=""):
    # One absolute deadline covers the decision and everything after it (history, notices, log lines); the one
    # the hook's entry point already started is kept. Whatever is still to do when it has passed is skipped.
    deadline = qr.DEADLINE.get()
    if deadline is None:
        deadline = time.monotonic() + qr.HOOK_BUDGET_SEC
    with qr.budget(deadline):
        with qr.collect_observations():
            result = prepare_decision(cmd, cwd, lead_key, session)
        # notices are fetched after the decision; a side-record error never changes the dispatch / deny result.
        if result and qr.within_budget("notices"):
            try:
                notice_session = session or "no session:" + str(lead_key or "default lead") + ":" + os.path.abspath(cwd)
                notices = qr.recent_notices(session=notice_session, limit=3)
                if notices:
                    if result.get("deny"):
                        result["deny"] = "\n".join(notices + [result["deny"]])
                    else:
                        result["notes"] = notices + result.get("notes", [])
            except Exception:
                pass
    return result


def prepare_decision(cmd, cwd, lead_key, session):
    # A family with no configured accounts is not managed: its dispatches run exactly as written.
    ds = [d for d in find_dispatches(cmd) if qr.accounts(d["fam"])]
    if not ds:
        return None
    # everything that only reads the command text happens here, under the parse limit; queries come after.
    limit = parse_limit()
    explicit = {}
    for d in ds:
        check_parse(limit)
        explicit[d["pos"]] = resolved_explicit(cmd, d)
        category, written = dispatch_model(cmd, d)
        allow = qr.ALLOW_CREDITS or credits_allowed(cmd, d["pos"], d["seg"])
        d.update(model=category, model_written=written, allow_credits=allow)
    failures = qr.recent_failures()
    if lead_key is None:
        lead_key = qr.detect_lead()[0]
    families = list(dict.fromkeys(d["fam"] for d in ds))
    resumes = {d["pos"]: resume_account(d["fam"], d["resume"], cwd) for d in ds if d["resume"]}
    raw_cache, rows_cache = {}, {}
    for fam in families:
        raw_cache[fam] = qr.read_family(fam, lead_key=lead_key)
    attempted = set()
    for d in ds:
        ex = explicit[d["pos"]]
        if not d["resume"] and (not ex or ex[0] == "ineffective"):
            qr.retry_blocked(d["fam"], raw_cache[d["fam"]], d["model"], d["allow_credits"], attempted)
    # score everything only after the re-queries, so one command never mixes readings from before and after.
    for d in ds:
        for model in dict.fromkeys((d["model"], d.get("pay_model") or d["model"])):
            key = (d["fam"], model, d["allow_credits"])
            if key not in rows_cache:
                rows_cache[key] = qr.score_rows(d["fam"], raw_cache[d["fam"]], model, d["allow_credits"])
    for fam in ("claude", "codex"):
        if fam not in raw_cache and qr.remaining() >= 1:
            try:
                extra = qr.read_family(fam, lead_key=lead_key)
                if qr.remaining() > 0:
                    raw_cache[fam] = extra
            except Exception:
                pass
        if fam in raw_cache:
            rows_cache[fam] = qr.score_rows(fam, raw_cache[fam], model="other", allow_credits=False)
    records = []
    with qr.state_lock("leases"):
        leases = qr.active_leases()
        for rs in rows_cache.values():
            qr.refresh_busy(rs, leases)
        result = decide_prepared(cmd, ds, lead_key, session, rows_cache, resumes, explicit, leases, records)
        if not result.get("deny"):
            qr.save("leases.json", leases)
    if result.get("deny"):
        records = [r for r in records if r[1] == "deny"]
    if records and qr.within_budget("decision-log"):
        for fam, mode, row, text, model in records:
            qr.log_decision("hook", fam, mode, row, text=text, cmd=cmd, lead=lead_key, session=session, model=model)
    if failures >= 3 and not result.get("deny"):
        result["notes"].append(t("fail.recent", count=failures))
    return result


def decide_prepared(cmd, ds, lead_key, session, rows_cache, resumes, explicit, leases, records):
    """Pure decision logic under the lease lock; on a deny none of this command's reservations are saved."""
    notes, inserts, pending = [], [], []
    selected = None
    primary = ds[0]

    def rows(d):
        return rows_cache[(d["fam"], d["model"], d["allow_credits"])]

    def pay_row(d, row):
        """The row the money check looks at: the same account scored for the model that decides what it would cost."""
        model = d.get("pay_model") or d["model"]
        if row is None or model == d["model"]:
            return row
        return qr.find_row(rows_cache[(d["fam"], model, d["allow_credits"])], row["key"]) or row

    def context():
        other = "codex" if primary["fam"] == "claude" else "claude"
        orows = rows_cache.get(other)
        extras = []
        # a failing extra line must not undo a deny that was already decided, nor suppress the other summary.
        for render in (lambda: qr.cross_hint(primary["fam"], rows(primary), best=selected, orows=orows),
                       lambda: qr.family_summary(other, orows) if orows else None):
            try:
                item = render()
                if item:
                    extras.append(item)
            except Exception:
                pass
        return extras

    def deny(d, message, row=None):
        nonlocal selected
        selected = None
        for lease in pending:
            leases.remove(lease)
        for rs in rows_cache.values():
            qr.refresh_busy(rs, leases)
        message = t("deny.header", index=ds.index(d) + 1, family=d["fam"], model=d["model"]) + "\n" + message
        records.append((d["fam"], "deny", row, message, d["model"]))
        # notes describing dispatches that will not happen are left out of the deny message; other reminders stay.
        dropped = [t("marker." + name) for name in ("auto", "keep_account", "as_specified", "dispatch_anyway", "will_charge", "will_incur")]
        reminders = [n for n in notes if not any(s in n for s in dropped)]
        return {"deny": qr.append_context(message, reminders + context())}

    def reserve(key):
        lease = {"key": key, "at": qr.now(), "session": session}
        leases.append(lease)
        pending.append(lease)
        for rs in rows_cache.values():
            qr.refresh_busy(rs, leases, pending)

    def suggestion(d):
        best = qr.choose(d["fam"], rows(d))[0]
        return t("suggest.best", reason=qr.reason(best)) if best else t("suggest.none")

    for d in ds:
        fam = d["fam"]
        label = "Claude" if fam == "claude" else "Codex"
        ex = explicit[d["pos"]]
        if fam == "claude" and not d["model_written"] and qr.SCARCE:
            notes.append(t("note.no_model", model=qr.SCARCE_LABEL) if not d["resume"] else t("note.resume_no_model", model=qr.SCARCE_LABEL))
        if d.get("model_dynamic") and qr.SCARCE:
            notes.append(t("note.model_dynamic", model=qr.SCARCE_LABEL))
        if ex and ex[0] == "ineffective":
            notes.append(t("note.ineffective", variable=config.ENV_DIR[fam], value=ex[1], label=label))
            ex = None
        if fam == "claude" and ex and ex[0] == qr.DEFAULT_KEY["claude"] and qr.account_key("claude", ex[1]) == ex[0]:
            notes.append(t("note.default_dir_explicit"))
        if d["resume"]:
            key = resumes[d["pos"]]
            row = qr.find_row(rows(d), key)
            if row is None:
                # the credit check still applies to an explicitly named account; an unknown original account is not assumed to be the lead's or the hook's.
                effective_row = pay_row(d, qr.find_row(rows(d), ex[0]) if ex else None)
                if effective_row and qr.credit_block(effective_row):
                    return deny(d, qr.credit_message(effective_row), effective_row)
                notes.append(t("note.resume_unknown", label=label))
                notes.append(t("note.resume_unknown_credits"))
                records.append((fam, "resume-unknown", None, "conversation location unknown", d["model"]))
                continue
            paying = pay_row(d, row)
            if qr.credit_block(paying):
                return deny(d, qr.credit_message(paying), paying)
            stale = any(t("marker.maybe_reset") in n or t("marker.presumed_reset") in n for n in row["notes"])
            gone = None if stale or row["paid"] and d["allow_credits"] else out_of_quota(row)
            if stale:
                notes.append(t("note.resume_stale", who=qr.who(row)))
            if gone:
                return deny(d, t("deny.resume_gone", label=label, who=qr.who(row), reason=gone, suggestion=suggestion(d)), row)
            if ex and ex[0] not in row["keys"]:
                return deny(d, t("deny.resume_mismatch", given=ex[1], who=qr.who(row)), row)
            if not ex:
                inserts.append((d["pos"], env_prefix(fam, key)))
            notes.append(t("note.resume_keep", who=qr.who(row)))
            if fam == "claude" and not d["model_written"] and qr.SCARCE:
                fb = (row.get("usage") or {}).get(qr.SCARCE)
                if fb and fb["used"] >= 100 and (fb.get("reset") is None or fb["reset"] > qr.now()):
                    notes.append(t("note.resume_scarce_out", model=qr.SCARCE_LABEL))
            if row["will_spend"] or paying["will_spend"]:
                notes.append(qr.spend_note(fam))
            reserve(key)
            if d is primary:
                selected = row
            records.append((fam, "resume", row, "kept the original account", d["model"]))
            continue
        if ex:
            key, val = ex
            row = qr.find_row(rows(d), key)
            if row is None:
                notes.append(t("note.explicit_unknown", value=val))
                records.append((fam, "explicit-unknown", None, "named a directory that is not in the pool", d["model"]))
                continue
            if qr.credit_block(row):
                return deny(d, qr.credit_message(row), row)
            reserve(key)
            if d is primary:
                selected = row
            text = "as specified"
            if row["blocked"]:
                notes.append(t("note.explicit_blocked", who=qr.who(row), reason=row["blocked"], suggestion=suggestion(d)))
                text += ", although it is blocked"
            elif row["wk_left"] is not None and row["wk_left"] < 30:
                best = qr.choose(fam, rows(d))[0]
                if best and key not in best["keys"] and not best["blocked"] and best["wk_left"] is not None and best["wk_left"] > row["wk_left"]:
                    notes.append(t("suggest.best_plain", reason=qr.reason(best)))
            if row["will_spend"]:
                notes.append(qr.spend_note(fam))
            records.append((fam, "explicit", row, text, d["model"]))
            continue
        best, warn, _ = qr.choose(fam, rows(d))
        if best is None:
            return deny(d, warn)
        reserve(best["key"])
        if d is primary:
            selected = best
        inserts.append((d["pos"], row_prefix(fam, best)))
        notes.append(t("note.auto", reason=qr.reason(best)) + (f"\n{warn}" if warn else ""))
        records.append((fam, "auto", best, "dispatched with a warning" if warn else "", d["model"]))
    notes.extend(context())
    new = cmd
    for pos, text in sorted(inserts, reverse=True):
        new = new[:pos] + text + new[pos:]
    return {"command": new if new != cmd else None, "notes": notes}


def read_event():
    """The hook's JSON from stdin as a dict, or None when it is not one. Claude Code sends UTF-8 on every platform."""
    stream = getattr(sys.stdin, "buffer", None)
    try:
        data = json.loads(stream.read().decode("utf-8", "replace")) if stream else json.load(sys.stdin)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def write_output(hso):
    text = json.dumps({"hookSpecificOutput": hso}, ensure_ascii=False)
    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:
        print(text)
        return
    sys.stdout.flush()
    stream.write(text.encode("utf-8") + b"\n")
    stream.flush()


def safe_text(key, fallback, **values):
    """A translated message, or plain English when the translation table itself is what broke."""
    try:
        return t(key, **values)
    except Exception:
        return fallback.format(**values)


# ---- the hard time limit ---------------------------------------------------------
# The budget is cooperative: every query, lock wait and parsing loop checks it, but a single read from a file
# system or a stdin that has stopped answering cannot be interrupted. The watchdog covers what is left: this
# long after the budget has run out the process ends without a decision, which lets the command run as written.
WATCHDOG_GRACE_SEC = 2


def hard_stop():
    """End the hook process now: nothing on stdout, one line on stderr, exit status 0."""
    try:
        os.write(2, b"quota-router: time limit reached; command left unchanged\n")
    except OSError:
        pass
    os._exit(0)


class Watchdog:
    def __init__(self, started):
        self.started, self.timer = started, None

    def arm(self, budget):
        """(Re)start the countdown: `budget` seconds from the hook's start, plus the grace period."""
        self.cancel()
        delay = self.started + budget + WATCHDOG_GRACE_SEC - time.monotonic()
        self.timer = threading.Timer(max(0.0, delay), lambda: hard_stop())
        self.timer.daemon = True
        self.timer.start()

    def cancel(self):
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None


def main():
    """Entry point of the hook. Whatever goes wrong in here the Bash command must still run:
    nothing on stdout (no decision), one line on stderr, exit status 0."""
    started = time.monotonic()
    watchdog = Watchdog(started)
    try:
        # reading stdin and the config file happens before the configured budget is known: the default applies.
        watchdog.arm(config.DEFAULTS["timing.hook_budget_seconds"])
        platform_compat.stdio_utf8(strict=True)
        respond(read_event(), started, watchdog)
    except Exception as e:
        qr.quiet_call(qr.log_error, "hook", e)
        try:
            print(f"quota-router: internal error ({type(e).__name__}); command left unchanged", file=sys.stderr)
        except Exception:
            pass
    finally:
        watchdog.cancel()


def respond(data, started=None, watchdog=None):
    if not data or data.get("tool_name") != "Bash":
        return
    if data.get("quota_router_self_test") is True:
        return self_test()
    ti = data.get("tool_input")
    ti = ti if isinstance(ti, dict) else {}
    cmd = ti.get("command")
    cmd = cmd if isinstance(cmd, str) else ""
    lowered = cmd.lower()  # cli_name() accepts CLAUDE.EXE, so this first look must too.
    if "claude" not in lowered and "codex" not in lowered:
        return
    # a missing or broken config must not get in the way: the command runs unchanged, with one line on stderr.
    try:
        qr.configure(config.load())
    except config.ConfigMissing as e:
        print(f'quota-router: no config at {e.path}, run "qr init"', file=sys.stderr)
        return
    except config.ConfigError as e:
        print(f"quota-router: config error in {e.path}: {e.detail}; command left unchanged", file=sys.stderr)
        return
    except Exception as e:
        print(f"quota-router: could not load config ({type(e).__name__}); command left unchanged", file=sys.stderr)
        return
    if watchdog:
        watchdog.arm(qr.HOOK_BUDGET_SEC)
    # the budget runs from the moment the hook started and stays in force until the answer is written.
    with qr.budget((time.monotonic() if started is None else started) + qr.HOOK_BUDGET_SEC):
        hso = answer(data, ti, cmd)
    if watchdog:
        watchdog.cancel()  # the decision is made; it must not be cut off halfway through being written.
    if hso:
        write_output(hso)


def self_test():
    """Reply to the harmless event `qr install` and `qr doctor` send to check that the installed command runs:
    one JSON object that is not a hook decision. Nothing is queried, reserved or logged."""
    try:
        config.load()
        state = "ok"
    except config.ConfigMissing:
        state = "missing"
    except Exception:
        state = "error"
    stream = getattr(sys.stdout, "buffer", None)
    text = json.dumps({"quota_router": "ok", "config": state})
    if stream is None:
        print(text)
        return
    sys.stdout.flush()
    stream.write(text.encode("utf-8") + b"\n")
    stream.flush()


def answer(data, ti, cmd):
    """The hookSpecificOutput for one dispatch command, or None when there is nothing to say."""
    lead_key = None
    session = data.get("session_id")
    session = session if isinstance(session, str) else ""
    cwd = data.get("cwd")
    try:
        lead_key = qr.detect_lead(data)[0]
        r = decide(cmd, cwd if isinstance(cwd, str) and cwd else os.getcwd(), lead_key=lead_key, session=session)
    except Exception as e:
        qr.quiet_call(qr.log_error, "hook", e)
        if qr.within_budget("decision-log"):
            qr.quiet_call(qr.log_decision, "hook", "?", "fail", text=type(e).__name__, cmd=cmd, lead=lead_key, session=session)
        print(f"quota-router: routing failed ({type(e).__name__}); command left unchanged", file=sys.stderr)
        r = {"command": None, "notes": [safe_text("note.failed", "[quota-router] Routing failed ({error}); running the command as written.",
                                                  error=type(e).__name__)]}
        failures = failure_count()
        if failures >= 3:
            r["notes"].append(safe_text("fail.recent", "Routing failed {count} time(s) in the last 24 hours", count=failures))
    if not r:
        return None
    hso = {"hookEventName": "PreToolUse"}
    if r.get("deny"):
        hso["permissionDecision"] = "deny"
        hso["permissionDecisionReason"] = r["deny"]
        # a decided deny is delivered even when counting earlier failures breaks or there is no time left for it.
        failures = failure_count()
        if failures >= 3:
            hso["additionalContext"] = safe_text("fail.recent", "Routing failed {count} time(s) in the last 24 hours", count=failures)
    else:
        if r.get("command"):
            hso["updatedInput"] = dict(ti, command=r["command"])
        if r.get("notes"):
            hso["additionalContext"] = "\n".join(r["notes"])
        if len(hso) == 1:
            return None
    return hso


def failure_count():
    if not qr.quiet_call(qr.within_budget, "failure-count", default=False):
        return 0
    return qr.quiet_call(qr.recent_failures, default=0)
