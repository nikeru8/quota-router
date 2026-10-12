"""Everything that depends on the operating system lives here.

macOS, Linux and Windows are implemented. Each function's docstring says what
differs per platform. The Windows branches are exercised by unit tests that
fake `sys.platform` and `msvcrt`, and for real by the CI matrix.
"""
import contextvars
import errno
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time


def system():
    """'darwin', 'linux' or 'windows'."""
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith(("win32", "cygwin")):
        return "windows"
    return "linux"


def home():
    return os.path.expanduser("~")


# ---- default locations -------------------------------------------------------
def default_config_dir(platform=None, env=None):
    """Directory that holds config.toml.

    macOS/Linux: ~/.config/quota-router. Windows: %APPDATA%\\quota-router
    (falls back to ~/AppData/Roaming when APPDATA is unset).
    """
    platform = platform or system()
    env = os.environ if env is None else env
    if platform == "windows":
        base = env.get("APPDATA") or os.path.join(home(), "AppData", "Roaming")
        return os.path.join(base, "quota-router")
    return os.path.join(home(), ".config", "quota-router")


def default_state_dir(platform=None, env=None):
    """Directory for cache, leases, logs and history.

    macOS/Linux: $XDG_STATE_HOME/quota-router, default ~/.local/state/quota-router.
    Windows: %LOCALAPPDATA%\\quota-router (falls back to ~/AppData/Local).
    """
    platform = platform or system()
    env = os.environ if env is None else env
    if platform == "windows":
        base = env.get("LOCALAPPDATA") or os.path.join(home(), "AppData", "Local")
        return os.path.join(base, "quota-router")
    base = env.get("XDG_STATE_HOME") or os.path.join(home(), ".local", "state")
    return os.path.join(base, "quota-router")


def default_launcher_dir(platform=None, env=None):
    """Directory of the `claude` launcher that `qr install` writes; it goes on PATH ahead of the real claude.

    macOS/Linux: $XDG_DATA_HOME/quota-router/bin, default ~/.local/share/quota-router/bin.
    Windows: %LOCALAPPDATA%\\quota-router\\bin (falls back to ~/AppData/Local).
    """
    platform = platform or system()
    env = os.environ if env is None else env
    if platform == "windows":
        base = env.get("LOCALAPPDATA") or os.path.join(home(), "AppData", "Local")
        return os.path.join(base, "quota-router", "bin")
    base = env.get("XDG_DATA_HOME") or os.path.join(home(), ".local", "share")
    return os.path.join(base, "quota-router", "bin")


def launcher_name(platform=None):
    """File name of the launcher: a POSIX sh script `claude`, or a batch file `claude.cmd` on Windows."""
    return "claude.cmd" if (platform or system()) == "windows" else "claude"


def executable_names(name, platform=None, env=None):
    """The file names a shell would accept for program `name`: the name itself, and on Windows every
    PATHEXT suffix (`claude.exe`, `claude.cmd`, …) in PATHEXT order."""
    if (platform or system()) != "windows":
        return [name]
    env = os.environ if env is None else env
    suffixes = [s for s in (env.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(";") if s]
    return [name + suffix.lower() for suffix in suffixes]


# ---- shell text ---------------------------------------------------------------
def shell_quote(path):
    """A path as exactly one word of a POSIX shell command, whatever characters it contains.

    Windows paths get forward slashes first: the Bash tool runs Git Bash there, where a
    backslash is an escape character and `C:/Users/me` is accepted as it is.
    """
    if system() == "windows":
        path = path.replace("\\", "/")
    return shlex.quote(path)


# ---- file lock ---------------------------------------------------------------
def try_lock(file):
    """Take an exclusive lock on an open file without waiting.

    Returns True when the lock was taken and False when another handle holds it.
    Any other failure raises OSError. The lock is released by unlock() or when the
    file is closed. Only ever used on dedicated `.lock` files, never on data files.
    macOS/Linux: flock(2), advisory. Windows: msvcrt.locking on byte 0, which is a
    mandatory lock on that byte and is taken at the file position, hence the seek.
    """
    if system() == "windows":
        import msvcrt
        os.lseek(file.fileno(), 0, os.SEEK_SET)
        try:
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return False
            raise
    import fcntl
    try:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def unlock(file):
    """Release a lock taken with try_lock()."""
    if system() == "windows":
        import msvcrt
        os.lseek(file.fileno(), 0, os.SEEK_SET)      # the same byte that was locked
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl
    fcntl.flock(file, fcntl.LOCK_UN)


def replace(source, target):
    """os.replace. On Windows it fails while another process has the target open, so it is retried briefly there."""
    attempts = 4 if system() == "windows" else 1
    for attempt in range(attempts):
        try:
            return os.replace(source, target)
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.02)


# ---- hard deadline around blocking I/O -----------------------------------------
def call_with_deadline(function, seconds, make_error):
    """Return function(), or raise make_error() once `seconds` have passed.

    A socket timeout only bounds each read, so a response that trickles in can
    outlive it; this bounds the whole call. The same on every platform: the call
    runs in a daemon worker thread and the caller waits for it with a timeout.
    A worker that is still running at the deadline is abandoned, not killed (a
    thread cannot be), so the function must carry its own socket timeout to end
    by itself soon after; its late result or exception is discarded. Works from
    any thread and uses no signals. An infinite `seconds` runs the function directly.
    Context variables of the caller (the time budget) are visible inside.
    """
    if not math.isfinite(seconds):
        return function()
    outcome = {}
    context = contextvars.copy_context()

    def work():
        try:
            outcome["value"] = context.run(function)
        except BaseException as error:
            outcome["error"] = error
    worker = threading.Thread(target=work, name="quota-router-deadline", daemon=True)
    worker.start()
    worker.join(max(0.0, seconds))
    if worker.is_alive():
        raise make_error()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


# ---- processes ---------------------------------------------------------------
# Only command lines that mention either CLI are asked for; that keeps the JSON small.
WINDOWS_PROCESS_QUERY = (
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
    "Get-CimInstance Win32_Process -Filter \"CommandLine LIKE '%claude%' OR CommandLine LIKE '%codex%'\" | "
    "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress")
WINDOWS_PROCESS_CACHE_SEC = 5
_windows_processes = None      # (monotonic time, result) of the last successful query in this process


def list_processes(run):
    """Processes as {pid: (parent pid, command line)}.

    `run(argv)` runs a command and returns a CompletedProcess, or None when it
    could not run or ran out of time (the caller supplies it so its time budget
    applies). Failure of any kind gives an empty dict: nobody is counted as busy.
    macOS/Linux: every process, from `ps`.
    Windows: the processes whose command line mentions claude or codex, from
    Get-CimInstance Win32_Process through PowerShell. Starting PowerShell costs
    roughly 0.5-2 s, so one successful answer is reused for a few seconds within
    the same process (the hook reads both families in one run).
    """
    if system() == "windows":
        global _windows_processes
        if _windows_processes and time.monotonic() - _windows_processes[0] < WINDOWS_PROCESS_CACHE_SEC:
            return dict(_windows_processes[1])
        result = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", WINDOWS_PROCESS_QUERY])
        if result is None or result.returncode != 0:
            return {}
        procs = parse_windows_processes(result.stdout)
        _windows_processes = (time.monotonic(), procs)
        return dict(procs)
    result = run(["ps", "-axww", "-o", "pid=,ppid=,command="])
    out = result.stdout if result else ""
    procs = {}
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(.*)", line)
        if m:
            procs[int(m[1])] = (int(m[2]), m[3])
    return procs


def parse_windows_processes(text):
    """ConvertTo-Json output → {pid: (ppid, command line)}. One process comes as an object, several as a list;
    CommandLine is null for processes the user may not inspect, and those are skipped."""
    try:
        data = json.loads(text) if text and text.strip() else []
    except ValueError:
        return {}
    procs = {}
    for item in data if isinstance(data, list) else [data]:
        try:
            if isinstance(item, dict) and isinstance(item.get("CommandLine"), str):
                procs[int(item["ProcessId"])] = (int(item.get("ParentProcessId") or 0), item["CommandLine"])
        except (KeyError, TypeError, ValueError):
            continue
    return procs


def process_environment(pid, command, run):
    """The environment of one process as a single 'A=1 B=2 …' string, or None when unknown.

    `command` is the command line list_processes() reported for it; it is used to
    cut the environment off the end of what `ps` prints. Only processes of the
    current user are readable. macOS/Linux: `ps eww`.
    Windows: always None. Another process's environment cannot be read there
    without debugging privileges; see directory_from_command_lines() for what is
    used instead.
    """
    if system() == "windows":
        return None
    result = run(["ps", "eww", "-o", "command=", "-p", str(pid)])
    if result is None:
        return None
    text = result.stdout.lstrip()
    if not text.startswith(command):
        return None
    return text[len(command):]


SHELL_PIECE = r"\"[^\"]*\"|'[^']*'|[^\s\"']+"
SHELL_WORD = re.compile(f"(?:{SHELL_PIECE})+")
CLI_PROGRAMS = ("claude", "codex", "node", "bun")


def assignment_before_program(text, variable):
    """(value or None, whether the CLI's program name was reached) for `VARIABLE=…` written as a prefix in a
    command line. Reading stops at the program: what follows it is the prompt, which may well mention the
    variable. A quoted word is read as a script of its own (`bash -c "VARIABLE=… codex exec …"`)."""
    def unquote(word):
        # one shell word can be several quoted and unquoted pieces written back to back, which is how the
        # hook itself quotes a directory with an apostrophe in it: 'C:/home/O'"'"'Brien/.claude-b'.
        return "".join(part[1:-1] if part[0] in "\"'" else part for part in re.findall(SHELL_PIECE, word))

    value = None
    for word in SHELL_WORD.findall(text):
        if word.startswith(variable + "="):
            value = unquote(word[len(variable) + 1:])
            continue
        if len(word) > 1 and word[0] == word[-1] and word[0] in "\"'" and word.count(word[0]) == 2:
            inner = word[1:-1]
            found, reached = assignment_before_program(inner, variable)
            if reached:
                return (found if found is not None else value), True
            if inner.startswith(variable + "="):
                value = inner[len(variable) + 1:]        # one argument quoted as a whole: "VARIABLE=C:/home/Jo Doe/x"
            continue
        name = re.split(r"[\\/]", unquote(word))[-1].lower()
        for suffix in (".exe", ".cmd", ".bat"):
            name = name[:-len(suffix)] if name.endswith(suffix) else name
        if name in CLI_PROGRAMS:
            return value, True
    return value, False


def directory_from_command_lines(pid, procs, variable):
    """Windows stand-in for process_environment(): the value of `VARIABLE=…` written as a prefix on the command
    line of the process or of one of its ancestors (`env CODEX_HOME=C:/x codex exec …`,
    `bash -c "CODEX_HOME=… codex exec …"`), nearest first, or None when no command line shows it.

    None means "unknown", not "default location": a variable that was exported earlier or inherited from the
    session is invisible here, so such a process is not counted against any account. Elsewhere this always
    returns None, because the real environment is readable.
    """
    if system() != "windows":
        return None
    seen = set()
    while pid in procs and pid not in seen and len(seen) < 8:
        seen.add(pid)
        parent, command = procs[pid]
        value = assignment_before_program(command, variable)[0]
        if value is not None:
            return value
        pid = parent
    return None


def spawn_detached(argv, env, stdout, stderr):
    """Start a background process that outlives the caller.

    macOS/Linux: a new session. Windows: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    (no console, not hit by the caller's Ctrl+C); the program is looked up on PATH
    first because CreateProcess only adds `.exe` by itself and npm installs `.cmd` shims.
    """
    if system() == "windows":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        program = shutil.which(argv[0], path=(env or os.environ).get("PATH")) or argv[0]
        subprocess.Popen([program, *argv[1:]], env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                         creationflags=flags, close_fds=True)
        return
    subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True)


# ---- paths ---------------------------------------------------------------------
def drive_path(path):
    """Windows only: Git Bash's spelling of a drive (`/c/work/x`) as the drive path it stands for (`c:/work/x`).
    Any other path, and every path on other systems, is returned as it is."""
    if system() == "windows":
        m = re.match(r"^/([A-Za-z])(/.*)?$", path)
        if m:
            return f"{m[1]}:{m[2] or '/'}"
    return path


def canonical(path):
    """A directory in the form used to compare two spellings of it: `~` expanded, links resolved, and on Windows
    case folded, separators unified and Git Bash's `/c/…` read as `C:\\…`."""
    return os.path.normcase(os.path.realpath(drive_path(os.path.expanduser(str(path)))))


def stdio_utf8(strict=False):
    """Windows consoles and pipes default to a legacy code page, which cannot encode some of the messages.
    strict=True (the hook: Claude Code reads UTF-8) switches the streams to UTF-8; otherwise the encoding is
    kept and unencodable characters are replaced instead of raising."""
    if system() != "windows":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8") if strict else stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


# ---- Claude Code login ---------------------------------------------------------
def keychain_service(config_dir):
    """macOS keychain item Claude Code uses for a config directory (None = default location)."""
    if config_dir is None:
        return "Claude Code-credentials"
    return "Claude Code-credentials-" + hashlib.sha256(config_dir.encode()).hexdigest()[:8]


def claude_credentials(config_dir, run):
    """Raw JSON text of Claude Code's stored login for a config directory, or None.

    `config_dir` None means the default location. Read-only: nothing is refreshed
    or written back. `run` is the same budget-aware runner as in list_processes().
    macOS: the login keychain, through `security find-generic-password`; when it
    has no entry (Claude Code itself falls back to a file when the keychain is
    locked, for example over SSH), <config_dir or ~/.claude>/.credentials.json.
    Linux and Windows: that file only.
    """
    if system() == "darwin":
        result = run(["security", "find-generic-password", "-s", keychain_service(config_dir), "-w"])
        if result is not None and result.returncode == 0 and result.stdout.strip():
            return result.stdout
    try:
        with open(credentials_file(config_dir), encoding="utf-8") as f:
            return f.read()
    except (OSError, ValueError):
        return None


def credentials_file(config_dir):
    return os.path.join(config_dir or os.path.join(home(), ".claude"), ".credentials.json")
