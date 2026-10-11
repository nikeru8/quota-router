"""`qr install`, `qr uninstall` and `qr doctor`.

install/uninstall edit one file, Claude Code's settings.json, and only the one
PreToolUse entry that belongs to this tool. doctor only looks: it checks that
files exist, never what is in a credential.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

from . import config, core, platform_compat
from .i18n import t

ENTRY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "__main__.py")
MINIMUM_PYTHON = (3, 11)


class SettingsError(Exception):
    pass


def hook_entry():
    """The hook in exec form: no shell is involved, so no quoting and no PATH lookup can go wrong.

    `command` is this interpreter's absolute path (on Windows a bare `python3` is often the Microsoft Store
    stub, which exits without running anything). -I keeps PYTHON* variables, the working directory and the
    user site-packages from changing what gets imported; -X utf8 makes the pipes UTF-8 on Windows.
    """
    return {"type": "command", "command": sys.executable, "args": [*INTERPRETER_FLAGS, ENTRY_FILE, "hook"]}


def same_path(a, b):
    """Two spellings of one path, compared as text: case and separators are folded on Windows, links are not followed."""
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


# ---- which entries are ours ---------------------------------------------------------
# `qr install` writes down what it put into each settings.json: the interpreter and the complete argument list.
# An entry is ours only when its command and its whole argument list are what was recorded, so that a hook
# which merely mentions a quota_router/__main__.py somewhere in its arguments (`python -c … __main__.py hook`)
# is never edited or removed. Without a record, only the exact invocation this package installs is recognised.
RECORD_FILE = "installs.json"
INTERPRETER_FLAGS = ["-I", "-X", "utf8"]


def record_path():
    return os.path.join(config.state_dir()[0], RECORD_FILE)


def read_record():
    try:
        with open(record_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def known_pairs(path):
    """[(python, argument list)] an entry in this settings file may consist of and still be ours: what an earlier
    `qr install` recorded for the file (possibly another interpreter or checkout), and what this interpreter
    would install now. None when nothing is recorded. A record written before the argument list was kept holds the entry file only; the arguments
    are then the ones that version installed."""
    saved = read_record().get(os.path.normcase(os.path.abspath(path)))
    if isinstance(saved, dict) and isinstance(saved.get("python"), str):
        args = saved.get("args")
        if not (isinstance(args, list) and all(isinstance(a, str) for a in args)):
            args = [*INTERPRETER_FLAGS, saved["entry"], "hook"] if isinstance(saved.get("entry"), str) else None
        if args:
            return [(saved["python"], args), (sys.executable, hook_entry()["args"])]
    return None   # nothing recorded for this file: is_ours() falls back to the exact invocation of this package


def remember(path, installed):
    """Record (installed=True) or forget what was put into this settings file. Failing to is not an error:
    the entry for the current interpreter and location is recognised without the record."""
    try:
        record = read_record()
        key = os.path.normcase(os.path.abspath(path))
        if installed:
            record[key] = {"python": sys.executable, "entry": ENTRY_FILE, "args": hook_entry()["args"]}
        elif record.pop(key, None) is None:
            return
        os.makedirs(os.path.dirname(record_path()), exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=RECORD_FILE + ".", suffix=".tmp", dir=os.path.dirname(record_path()))
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=1)
            platform_compat.replace(temporary, record_path())
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except OSError:
        pass


def is_ours(hook, pairs=None):
    """An exec-form entry that is, word for word, an invocation `qr install` wrote.

    With `pairs` (see known_pairs): the command is a recorded Python path and the arguments are the complete
    recorded list. Without: the command's file name is a Python interpreter and the arguments are exactly
    `-I -X utf8 <this package's __main__.py> hook`. Anything else that has the entry file somewhere among its
    arguments runs something this tool did not install."""
    if not isinstance(hook, dict) or not isinstance(hook.get("command"), str) or not isinstance(hook.get("args"), list):
        return False
    args = hook["args"]
    if not all(isinstance(a, str) for a in args):
        return False
    if pairs is None:
        name = os.path.basename(hook["command"].replace("\\", "/")).lower()
        python = same_path(hook["command"], sys.executable) or re.fullmatch(r"(python|pypy)[0-9.]*w?(\.exe)?", name)
        return bool(python) and same_args(args, hook_entry()["args"])
    return any(same_path(hook["command"], python) and same_args(args, recorded) for python, recorded in pairs)


def same_args(args, recorded):
    """The same argument list. The entry file is compared as a path (case and separators fold on Windows)."""
    return len(args) == len(recorded) and all(
        a == b or (os.path.isabs(b) and same_path(a, b)) for a, b in zip(args, recorded))


def is_foreign(hook, pairs=None):
    """An exec-form entry that runs some quota_router/__main__.py as `hook` but is not one of ours: another
    installation's, or one whose record is gone. It is reported and never edited or removed."""
    if not isinstance(hook, dict) or not isinstance(hook.get("args"), list) or is_ours(hook, pairs):
        return False
    args = [a for a in hook["args"] if isinstance(a, str)]
    return bool(args) and args[-1] == "hook" and any(
        re.search(r"(?:^|[\\/])quota_router[\\/]__main__\.py$", a) for a in args)


def is_manual(hook):
    """A hand-written shell-form entry whose whole command is `qr hook`, optionally through a Python interpreter
    (`/path/to/bin/qr hook`, `python3 /path/to/bin/qr hook`). It is reported, never edited or removed."""
    if not isinstance(hook, dict) or "args" in hook or not isinstance(hook.get("command"), str):
        return False
    try:
        words = shlex.split(hook["command"].replace("\\", "/"))
    except ValueError:
        return False
    while words and re.match(r"[A-Za-z_][A-Za-z0-9_]*=", words[0]):
        words.pop(0)
    if len(words) > 2 and re.fullmatch(r"(python[0-9.]*|py)(\.exe)?", os.path.basename(words[0]).lower()):
        rest, words = words[1:], []
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in ("-X", "-W") else rest[1:]
        words = rest
    return len(words) == 2 and os.path.basename(words[0]).lower() in ("qr", "qr.exe") and words[1] == "hook"


def covers_bash(group):
    """Whether a matcher group applies to the Bash tool: no matcher, `*`, `Bash`, or a pattern such as `Bash|Write`."""
    matcher = group.get("matcher") if isinstance(group, dict) else None
    if matcher is None or matcher in ("", "*"):
        return True
    if not isinstance(matcher, str):
        return False
    try:
        return re.fullmatch(matcher, "Bash") is not None
    except re.error:
        return False


def settings_path(config_dir=None):
    return os.path.join(config_dir or config.default_dir("claude"), "settings.json")


def read_settings(path):
    """(data, original text). A missing or empty file is an empty object; anything unreadable raises SettingsError."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as error:
        raise SettingsError(getattr(error, "strerror", None) or type(error).__name__) from None
    if not text.strip():
        return {}, text
    try:
        data = json.loads(text)
    except ValueError as error:
        raise SettingsError(t("install.not_json", detail=error)) from None
    if not isinstance(data, dict):
        raise SettingsError(t("install.not_object"))
    return data, text


def groups_of(data):
    """The PreToolUse matcher groups, after checking that the surrounding structure is what Claude Code documents."""
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise SettingsError(t("install.bad_shape", name="hooks"))
    groups = hooks.get("PreToolUse", [])
    if not isinstance(groups, list):
        raise SettingsError(t("install.bad_shape", name="hooks.PreToolUse"))
    return groups


def entries(data):
    """[(group, index in group["hooks"], hook)] for every hook under PreToolUse."""
    found = []
    for group in groups_of(data):
        if isinstance(group, dict) and isinstance(group.get("hooks"), list):
            found.extend((group, index, hook) for index, hook in enumerate(group["hooks"]))
    return found


def write_settings(path, data, original):
    """Back the file up next to itself, then replace it in one step. Indentation and the final newline are kept."""
    indent = 2
    if original:
        m = re.search(r"\n([ \t]+)\"", original)
        if m:
            indent = m[1] if "\t" in m[1] else len(m[1])
    text = json.dumps(data, indent=indent, ensure_ascii=False)
    if original is None or original.endswith("\n") or not original.strip():
        text += "\n"
    directory = os.path.dirname(path)
    backup = None
    try:
        os.makedirs(directory, exist_ok=True)
        if original is not None:
            backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
            number = 1
            while os.path.exists(backup):
                number += 1
                backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}-{number}"
            shutil.copy2(path, backup)
        handle, temporary = tempfile.mkstemp(prefix="settings.json.", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            if original is not None:
                shutil.copymode(path, temporary)
            platform_compat.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except OSError as error:
        raise SettingsError(error.strerror or type(error).__name__) from None
    return backup


def install(config_dir=None):
    """Add the hook to settings.json. Returns (status, path, backup); status is 'added', 'updated', 'unchanged',
    'manual' or 'foreign' (the last two: another entry already runs the hook, nothing was changed)."""
    path = settings_path(config_dir)
    data, original = read_settings(path)
    wanted = hook_entry()
    pairs = known_pairs(path)
    found = entries(data)
    ours = [(group, index) for group, index, hook in found if is_ours(hook, pairs)]
    if not ours:
        for status, test in (("foreign", lambda hook: is_foreign(hook, pairs)), ("manual", is_manual)):
            if any(test(hook) and covers_bash(group) for group, _, hook in found):
                return status, path, None
        data.setdefault("hooks", {}).setdefault("PreToolUse", []).append({"matcher": "Bash", "hooks": [wanted]})
        backup = write_settings(path, data, original)
        remember(path, True)
        return "added", path, backup
    # Ours, but possibly left by another interpreter or checkout, duplicated, or under a matcher that never
    # sees Bash: the first one that Bash reaches is brought up to date in place, every other copy is dropped.
    keep = next(((group, index) for group, index in ours if covers_bash(group)), None)
    changed = keep is None or len(ours) > 1 or keep[0]["hooks"][keep[1]] != wanted
    if keep:
        keep[0]["hooks"][keep[1]] = wanted
    emptied = []
    for group, index in reversed(ours):
        if (group, index) != keep:
            del group["hooks"][index]
            emptied.append(group)
    prune(data, emptied)
    if keep is None:
        data.setdefault("hooks", {}).setdefault("PreToolUse", []).append({"matcher": "Bash", "hooks": [wanted]})
    if not changed:
        remember(path, True)
        return "unchanged", path, None
    backup = write_settings(path, data, original)
    remember(path, True)
    return "updated", path, backup


def prune(data, emptied):
    """Drop the matcher groups in `emptied` (those an entry of ours was just removed from) that are left without
    hooks, and the containers that were left empty by that. Groups that were already empty are not touched."""
    hooks = data.get("hooks")
    groups = hooks.get("PreToolUse") if isinstance(hooks, dict) else None
    if not isinstance(groups, list):
        return
    before = len(groups)
    groups[:] = [g for g in groups if not (any(g is e for e in emptied) and g.get("hooks") == [])]
    if not groups and before:
        del hooks["PreToolUse"]
        if not hooks:
            del data["hooks"]


def uninstall(config_dir=None):
    """Remove the entry install() added and nothing else. Returns (status, path, backup); status is 'removed' or 'absent'."""
    path = settings_path(config_dir)
    data, original = read_settings(path)
    pairs = known_pairs(path)
    ours = [(group, index) for group, index, hook in entries(data) if is_ours(hook, pairs)]
    if not ours:
        return "absent", path, None
    for group, index in reversed(ours):
        del group["hooks"][index]
    prune(data, [group for group, _ in ours])
    backup = write_settings(path, data, original)
    remember(path, False)
    return "removed", path, backup


def installed_state(config_dir=None):
    """(state, hook that runs for Bash or None). State: 'installed'; 'outdated' (ours, but for another interpreter
    or location, or duplicated); 'misplaced' (ours, but under a matcher that does not cover Bash); 'manual';
    'foreign' (another installation's entry); 'absent'; 'unreadable'."""
    path = settings_path(config_dir)
    try:
        data, _ = read_settings(path)
        found = entries(data)
    except SettingsError:
        return "unreadable", None
    pairs = known_pairs(path)
    ours = [(group, hook) for group, _, hook in found if is_ours(hook, pairs)]
    if ours:
        reached = [hook for group, hook in ours if covers_bash(group)]
        if not reached:
            return "misplaced", None
        return ("installed" if len(ours) == 1 and reached[0] == hook_entry() else "outdated"), reached[0]
    for state, test in (("foreign", lambda hook: is_foreign(hook, pairs)), ("manual", is_manual)):
        hook = next((hook for group, _, hook in found if test(hook) and covers_bash(group)), None)
        if hook:
            return state, hook
    return "absent", None


# ---- running the installed command once -----------------------------------------------
SELF_TEST_EVENT = {"tool_name": "Bash", "tool_input": {"command": "true"}, "quota_router_self_test": True}
SELF_TEST_TIMEOUT = 20


def run_hook(hook):
    """Run a hook entry the way Claude Code would, once, with an event that makes it do nothing but answer.
    Returns (ok, detail, first lines of stderr); ok is None when the entry could not be tried here."""
    if isinstance(hook.get("args"), list):
        argv = [hook["command"], *[a for a in hook["args"] if isinstance(a, str)]]
    else:
        shell = shutil.which("bash") if platform_compat.system() == "windows" else "/bin/sh"
        if not shell:
            return None, t("doctor.run.no_shell"), []
        argv = [shell, "-c", hook["command"]]
    try:
        result = subprocess.run(argv, input=json.dumps(SELF_TEST_EVENT).encode("utf-8"), capture_output=True,
                                timeout=SELF_TEST_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, t("doctor.run.timeout", seconds=SELF_TEST_TIMEOUT), []
    except OSError as error:
        return False, t("doctor.run.not_started", detail=error.strerror or type(error).__name__), []
    stderr = [line.strip()[:200] for line in result.stderr.decode("utf-8", "replace").splitlines() if line.strip()][:3]
    try:
        reply = json.loads(result.stdout.decode("utf-8"))
    except ValueError:
        reply = None
    if isinstance(reply, dict) and reply.get("quota_router") == "ok":
        return True, "", stderr
    return False, t("doctor.run.not_json", code=result.returncode), stderr


# ---- command line ---------------------------------------------------------------
def option(argv, name):
    """Value of `--name DIR` / `--name=DIR`, or None. A flag without a value ends the command with usage."""
    for index, arg in enumerate(argv):
        if arg == name:
            if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
                print(t("usage.install"), file=sys.stderr)
                raise SystemExit(2)
            return config.expand_dir(argv[index + 1])
        if arg.startswith(name + "="):
            return config.expand_dir(arg.split("=", 1)[1])
    return None


def run(action, argv):
    config_dir = option(argv, "--config-dir")
    try:
        status, path, backup = action(config_dir)
    except SettingsError as error:
        print(t("install.failed", path=settings_path(config_dir), detail=error), file=sys.stderr)
        raise SystemExit(2)
    print(t("install." + status, path=path))
    if backup:
        print(t("install.backup", path=backup))
    if status in ("added", "updated", "unchanged"):
        # the file is in place; now see whether the command in it actually starts and answers.
        ok, detail, stderr = run_hook(hook_entry())
        if not ok:
            print("WARN  " + t("doctor.run.bad", path=path, detail=detail), file=sys.stderr)
            for text in stderr:
                print("WARN    " + t("doctor.run.stderr", text=text), file=sys.stderr)
            # settings.json is written, but a hook that does not answer routes nothing: scripts must be able to tell.
            raise SystemExit(1)
    if status in ("added", "updated"):
        print(t("install.next"))


def install_command(argv):
    run(install, argv)


def uninstall_command(argv):
    run(uninstall, argv)


def doctor(argv):
    """One line per check, each starting with OK or WARN. Always exits 0: it reports, it does not judge."""
    def line(ok, text):
        print(("OK    " if ok else "WARN  ") + text)

    version = ".".join(str(part) for part in sys.version_info[:3])
    line(sys.version_info[:2] >= MINIMUM_PYTHON, t("doctor.python", version=version, path=sys.executable,
                                                   minimum=".".join(str(part) for part in MINIMUM_PYTHON)))
    line(True, t("doctor.platform", system=platform_compat.system()))
    path = config.config_path()[0]
    cfg = None
    try:
        cfg = config.load()
        line(True, t("doctor.config_ok", path=path, count=len(cfg.accounts)))
        for warning in cfg.warnings:
            line(False, t("doctor.config_warning", text=warning))
    except config.ConfigMissing:
        line(False, t("doctor.config_missing", path=path))
    except config.ConfigError as error:
        line(False, t("doctor.config_error", path=path, detail=error.detail))
    state = config.state_dir()[0]
    lock_ok, lock_detail = check_lock(state)
    line(lock_ok is not None, t("doctor.state_ok", path=state) if lock_ok is not None else t("doctor.state_bad", path=state, detail=lock_detail))
    if lock_ok is not None:
        line(lock_ok, t("doctor.lock_ok") if lock_ok else t("doctor.lock_bad", detail=lock_detail))
    procs = None
    try:
        procs = platform_compat.list_processes(core.run_local)
    except Exception:
        pass
    line(bool(procs), t("doctor.processes_ok", count=len(procs)) if procs else t("doctor.processes_bad"))
    if platform_compat.system() == "windows":
        line(False, t("doctor.windows_busy"))
        bash = shutil.which("bash")
        line(bool(bash), t("doctor.bash_ok", path=bash) if bash else t("doctor.bash_missing"))
    for family in config.FAMILIES:
        found = shutil.which(family)
        if cfg is None or cfg.family(family):
            line(bool(found), t("doctor.cli_ok", name=family, path=found) if found else t("doctor.cli_missing", name=family))
    hook_dirs = [None]
    for account in cfg.accounts if cfg else []:
        directory = account["dir"] or config.default_dir(account["family"])
        label = f"{account['family']} {account['key']}"
        if not os.path.isdir(directory):
            line(False, t("doctor.dir_missing", account=label, path=directory))
            continue
        if account["family"] == "claude":
            credentials = platform_compat.credentials_file(account["dir"])
            if os.path.isfile(credentials):
                line(True, t("doctor.login_file", account=label, path=credentials))
            elif platform_compat.system() == "darwin":
                line(True, t("doctor.login_keychain", account=label, path=directory))
            else:
                line(False, t("doctor.login_missing", account=label, path=credentials))
            if account["dir"] is not None:
                hook_dirs.append(account["dir"])
        else:
            credentials = os.path.join(directory, "auth.json")
            line(os.path.isfile(credentials), t("doctor.login_file", account=label, path=credentials)
                 if os.path.isfile(credentials) else t("doctor.login_missing", account=label, path=credentials))
    for directory in hook_dirs:
        status, hook = installed_state(directory)
        target = settings_path(directory)
        hint = "qr install" + ("" if directory is None else " --config-dir " + platform_compat.shell_quote(directory))
        line(status in ("installed", "manual"), t("doctor.hook." + status, path=target, command=hint))
        if hook is None:
            continue
        # being listed is not the same as working: run the listed command once with a harmless event.
        ok, detail, stderr = run_hook(hook)
        line(bool(ok), t("doctor.run.ok", path=target) if ok else t("doctor.run.bad", path=target, detail=detail))
        for text in ([] if ok else stderr):
            line(False, "  " + t("doctor.run.stderr", text=text))


def check_lock(state):
    """(True, "") when a lock can be taken and is exclusive; (False, why) when not; (None, why) when the
    state directory itself cannot be used.

    Two handles of this one process are enough on every platform: flock() locks belong to the open file, and
    Windows byte-range locks to the handle, so the second handle is refused exactly as another process would be."""
    def release(file):
        # the verdict is already known when this runs; a handle that turns out not to hold the lock must not replace it.
        try:
            platform_compat.unlock(file)
        except OSError:
            pass

    try:
        os.makedirs(state, exist_ok=True)
        path = os.path.join(state, "doctor.lock")
        with open(path, "a", encoding="utf-8") as first, open(path, "a", encoding="utf-8") as second:
            try:
                if not platform_compat.try_lock(first):
                    return False, t("doctor.lock_held")
                try:
                    if platform_compat.try_lock(second):
                        release(second)
                        return False, t("doctor.lock_not_exclusive")
                finally:
                    release(first)
            except Exception as error:
                return False, type(error).__name__
        try:
            os.unlink(path)
        except OSError:
            pass
        return True, ""
    except OSError as error:
        return None, error.strerror or type(error).__name__
