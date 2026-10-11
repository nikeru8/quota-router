# Windows portability research

Date checked: 2026-10-11. Sources fetched or searched on that date are listed under each answer.

## 1. Claude Code OAuth credentials on native Windows (confidence: high)

- Windows does **not** use an OS keychain. Credentials are stored in `%USERPROFILE%\.claude\.credentials.json`, which inherits the ACL of the user profile directory. Unlike Linux, there is no explicit `0600` mode.
- If `CLAUDE_CONFIG_DIR` is set, `.credentials.json` goes under that directory instead. This is documented for all platforms. On macOS the Keychain entry is also keyed to the config dir, so each config dir gets its own entry.
- macOS fallback: if the Keychain rejects the write (for example, it is locked over SSH), the login goes to `~/.claude/.credentials.json` with mode 0600.
- Caveat: a Console sign-in made without an API key is stored as an Anthropic profile in `%APPDATA%\Anthropic`, outside the config dir. Separate `CLAUDE_CONFIG_DIR`s do not keep those sign-ins apart.
- Implication for quota-router: on Windows, reading a Claude account's token means reading `<config dir>\.credentials.json`, the same code path as Linux.

Source: https://code.claude.com/docs/en/iam ("Credential management", "Log in with multiple accounts")

## 2. Shell used by the Bash tool and by hook commands on Windows (confidence: high for the defaults, medium for the quirks)

- **Bash tool:** uses Git Bash when Git for Windows is installed. If Claude Code can't find it, set `env.CLAUDE_CODE_GIT_BASH_PATH` (for example `C:\Program Files\Git\bin\bash.exe`). Without Git for Windows, there is no Bash tool and Claude Code uses the PowerShell tool. With Git installed, the PowerShell tool is also on by default for claude.ai and Console accounts.
- **Hook commands** (`type: "command"`) run in one of two forms:
  - **Shell form** (no `args`): the `command` string goes to `sh -c` on macOS and Linux, to **Git Bash on Windows**, or to PowerShell if Git Bash isn't installed. The per-hook field `"shell": "bash" | "powershell"` overrides this.
  - **Exec form** (`args` present): no shell is used. `command` is resolved on PATH and spawned directly. On Windows it must be a real `.exe`; `.cmd` and `.bat` shims (npm, npx, and similar) cannot be spawned this way.
- **Quoting:** in shell form, wrap path placeholders in double quotes, for example `"node \"${CLAUDE_PLUGIN_ROOT}\"/x.js"`. The docs recommend exec form whenever a path placeholder is involved.
- **python3 vs python:** on Windows, `python3` often resolves to the Microsoft Store App Execution Alias stub. In a non-interactive hook it can exit with code 49 and no output, so the hook is silently skipped (community issue report). Use `python` or `py -3`, or an absolute interpreter path. The most robust option is exec form: `"command": "C:\\...\\python.exe", "args": ["path\\to\\hook.py"]`.
- **PATH:** some Git installs put only `Git\cmd` on PATH, not `Git\bin`. A community write-up reports hooks then failing silently because `bash` can't be resolved.
- **Path separators:** Git Bash accepts both `C:/x` and `/c/x`. Python receives whatever string it is given. Use `pathlib` and don't hardcode `/`. `claude --debug hooks` shows how each hook was executed.

Sources: https://code.claude.com/docs/en/setup ("Install on native Windows"), https://code.claude.com/docs/en/hooks (command hook fields, exec form and shell form), https://claudeissues.com/issue/57946-windows-git-bash-pretooluse-hook-enforcement-silently-bypassed-hooks-exit-49-wit, https://dev.to/localplugins/your-claude-code-hook-doesnt-fire-on-windows-and-nothing-tells-you-why-2dg6

## 3. Codex CLI on Windows (confidence: medium-high)

- **Native support:** OpenAI's Windows page says Codex runs natively in PowerShell with a Windows sandbox and lists the CLI as a supported Windows surface. WSL is optional, for Linux toolchains. The page doesn't say "CLI runs natively" in so many words, but it doesn't require WSL.
- **`CODEX_HOME`:** defaults to `~/.codex`, which on Windows is `%USERPROFILE%\.codex`. The official page's example value is `C:\Users\alice\.codex`, and sandbox logs go to `CODEX_HOME\.sandbox\sandbox.log`.
- **`auth.json`:** lives at `CODEX_HOME\auth.json`, unless credential storage is set to the OS credential store. Third-party sources report the keyring entry is keyed by the `CODEX_HOME` path.

Sources: https://learn.chatgpt.com/docs/windows/windows-sandbox (redirected from developers.openai.com/codex/windows), https://developers.openai.com/codex/auth, https://codex.danielvaughan.com/2026/04/01/codex-cli-authentication-flows-credential-management/

## 4. Cross-platform file lock and Windows-safe deadlines in Python (confidence: high; based on stdlib documentation, https://docs.python.org/3/library/msvcrt.html and https://docs.python.org/3/library/fcntl.html)

### File lock

```python
import os, time
if os.name == "nt":
    import msvcrt
    def _lock(f, blocking):
        f.seek(0)
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        while True:
            try:
                msvcrt.locking(f.fileno(), mode, 1)   # lock byte 0
                return
            except OSError:
                if not blocking:
                    raise
                # LK_LOCK gives up after ~10 s; keep retrying
                time.sleep(0.05)
    def _unlock(f):
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl
    def _lock(f, blocking):
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    def _unlock(f):
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)

class FileLock:
    def __init__(self, path):
        self.path = path
    def __enter__(self):
        self.f = open(self.path, "a+b")   # creates the file, never truncates it
        _lock(self.f, True)
        return self
    def __exit__(self, *a):
        try:
            _unlock(self.f)
        finally:
            self.f.close()
```

Pitfalls:
- `msvcrt.locking` locks a byte range that starts at the **current file position**. Always `seek(0)` before both locking and unlocking, and unlock the same range you locked.
- `LK_LOCK` retries 10 times, once per second, then raises `OSError`. It is not an indefinite wait, so wrap it in your own retry loop or deadline.
- Windows locks are mandatory: other processes cannot read or write the locked byte. Use a **separate `.lock` file**, not the data file itself.
- `"a+b"` mode: on POSIX, appends ignore `seek`, which doesn't matter for a lock file. Don't use `"w"`, because it truncates.
- `flock` locks are per open file description and are released when the descriptor closes. They are unreliable on NFS. Windows locks are released when the handle closes, but the docs advise unlocking explicitly.
- The lock does not protect the data. For the data file, write to a temp file, then `os.replace()`, which is atomic on the same volume on both OSes. On Windows, `os.replace` can fail with `PermissionError` if another process holds the target open, so add a short retry.

### Replacing `SIGALRM` absolute deadlines

`signal.SIGALRM` doesn't exist on Windows. The standard replacement is to compute the deadline yourself and pass the remaining time to the call:

```python
deadline = time.monotonic() + TOTAL
def remaining():
    r = deadline - time.monotonic()
    if r <= 0:
        raise TimeoutError
    return r
urllib.request.urlopen(req, timeout=remaining())
```

- `timeout=` is per socket operation (connect, then each read), not a total. For a hard total limit, run the request in a worker: `concurrent.futures.ThreadPoolExecutor(1).submit(fetch).result(timeout=remaining())`.
- On timeout, abandon the worker thread; it can't be killed. Make it a daemon thread, or call `executor.shutdown(wait=False, cancel_futures=True)`. Keep a per-socket `timeout` too, so the thread eventually dies.
- `threading.Timer` that closes the response or socket also works, but it is more fragile.
- Alternative: a subprocess with `subprocess.run(..., timeout=)`, which actually kills the child process.

## 5. Listing processes with command lines on Windows (confidence: medium-high)

Recommendation: one PowerShell call that returns JSON.

```
powershell.exe -NoProfile -NonInteractive -Command "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress"
```

Parse the output with `json.loads`.

- **Cost:** about 0.5–2 s wall time, mostly PowerShell startup and the WMI query. `pwsh` (PowerShell 7) starts somewhat faster. This is acceptable for a cached status check, but not for a hook that runs on every tool call.
- **Mitigations:**
  - Add `-Filter "Name='claude.exe' or Name='node.exe' or Name='codex.exe'"` to shrink the result.
  - Cache the result for a few seconds.
  - Treat `CommandLine` as possibly `null` for other users' or elevated processes when not running as admin.
- **Avoid:**
  - `wmic`: deprecated, and removed by default in newer Windows 11 builds.
  - `tasklist`: no command lines.
- **Zero-dependency faster path:** `ctypes` with `NtQueryInformationProcess` / `ReadProcessMemory` on the PEB. This is complex and brittle; not worth it here.

Evidence: general Microsoft guidance (wmic deprecation, CIM cmdlets). I did not fetch a specific page this session, so treat the timing figures as estimates.

## 6. GitHub Actions matrix (confidence: high)

```yaml
name: tests
on: [push, pull_request]
jobs:
  test:
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest, windows-latest]
        python-version: ["3.11", "3.13"]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - run: python -m pip install pytest
      - run: python -m pytest -q
```

Notes:
- Quote the versions. Unquoted, `3.10` would be parsed as the number 3.1; it doesn't matter for 3.11 and 3.13, but quoting is the safe habit.
- `windows-latest` runs steps in `pwsh` by default. Use `python -m pytest`, not `python3`.
- Newer major versions of `actions/checkout` and `actions/setup-python` may exist; I didn't verify the current tags.

## Could not verify

- Whether Claude Code on Windows has any optional Credential Manager or DPAPI storage. The docs only mention the plain file.
- An official OpenAI page that states the Windows `auth.json` path. The current CLI default for `cli_auth_credentials_store` (`file`, `keyring` or `auto`) on Windows.
- Whether the closed `python3` exit-49 issue (#57946) was fixed in Claude Code itself or closed as "won't fix".
- Measured timings for `Get-CimInstance` on real hardware.
- The current major versions of `actions/checkout` and `actions/setup-python`.
