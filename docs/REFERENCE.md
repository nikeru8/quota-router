# quota-router reference

Details that are too fine for the README: how config values are validated, what exactly the log files hold, how `qr install` recognises its own entry, what `qr doctor` checks, how the time limit works, what differs on Windows, and which tests are skipped where. For installing and everyday use, start with the [README](../README.md).

## Config file

Location: `$QR_CONFIG`, else `~/.config/quota-router/config.toml` (Windows: `%APPDATA%\quota-router\config.toml`). The README lists every setting with its default.

### Validation

| Input | Result |
|---|---|
| Unknown top-level section, unknown setting, unknown field in an `[[accounts]]` table, unknown table under `[families]` | Warning; the value is ignored. Warnings are printed by `qr config` (stderr) and `qr doctor` only. The hook and the other commands load the file silently. |
| Wrong type (number where a string is expected and so on), negative number, `inf` or `nan` | Error. |
| `general.language` other than `en` / `zh-TW` | Error. |
| `endpoints.*` not starting with `http://` or `https://` | Error. |
| No `[[accounts]]`, an account without `family` or `key`, `family` other than `claude` / `codex`, a key with characters outside letters, digits, `-`, `_`, `.`, a duplicate key | Error. |
| `dir` that is not absolute after expansion | Error. |
| Two accounts of one family at the default location (`dir = ""`, or a `dir` that resolves to `~/.claude` / `~/.codex`) | Error. |
| File is not UTF-8 or not valid TOML | Error. |

On an error, `qr status`, `qr pick`, `qr check`, `qr report`, `qr decisions`, `qr leases` and `qr config` print one line and exit with status 2. `qr doctor` reports the error as a `WARN` line and still exits 0. `qr install` and `qr uninstall` do not need a usable config at all. The hook prints one line on stderr and lets the command run unchanged.

### `dir`

- `~` and `$HOME` (any environment variable, in fact) are expanded when the file is loaded.
- **Windows:** `C:\…`, `C:/…`, `~/…` and the Git Bash spelling `/c/…` are all accepted. `/c/…` is converted to `C:\…` when the file is loaded; this conversion happens on Windows only, since on other systems `/c/…` is an ordinary path.
- A Claude account at the default location is run with `CLAUDE_CONFIG_DIR` *unset* (`env -u CLAUDE_CONFIG_DIR claude …`), not with the variable pointing at `~/.claude`. A Codex account always gets `CODEX_HOME=<dir>`.

### `qr init`

Lists the default location of each family first, whether or not anything exists there, then every `~/.claude-*` and `~/.codex-*` directory that contains one of these marker files or directories: `.claude.json`, `.credentials.json`, `settings.json`, `projects` (Claude); `auth.json`, `config.toml`, `sessions` (Codex). It checks that they exist and reads none of them, so **an account in the generated file is not necessarily logged in**. Keys: the directory name without the dot for Claude (`.claude-b` → `claude-b`), `codex-2`, `codex-3`, … for Codex; a key that would collide or hold an invalid character is adjusted (`claude-main-2`). `[endpoints]` is written commented out so that a later change of the default addresses is not pinned by old config files.

### Constants that are not settings

Tolerance when comparing reset dates: 60 seconds. History file size cap: 2 MB. Observation index retention: 30 days. Back-off after a failed usage query: 60 s doubling to 15 minutes (a `Retry-After` header wins). Score scaling for a tight five-hour window starts under 30%. The "about to expire" hint needs at least 30% left and a reset within 24 hours.

## Selection details

The README's "How it decides" is the summary. The parts that surprise people:

- **Unknown readings.** An account whose quota cannot be read (network failure, expired token, no local numbers) is not skipped. It gets the median score of the accounts that are known, so it can be picked, with a note. The lead's own account is the exception: unknown means protected.
- **Old readings.** When every account is blocked, readings that are not fresh are queried once more. If an account's numbers are still old after that and it is not the lead's and not at risk of spending money, it is dispatched to anyway with a `⚠` warning that the numbers are old: quota may have reset since.
- **Not logged in.** An account whose directory holds no credentials, and for which there is no earlier reading, is never picked (`not logged in`). Naming it explicitly still runs the command as written, with a warning.
- **Order of preference.** Accounts that would not spend money come before accounts that would (only relevant once spending is allowed); then the higher score; then the earlier position in the config file.
- **Account order** also decides which directory represents two Claude directories logged in to the same account (the first one that is not the lead's), and which account is queried first when the time budget cannot cover all of them (the oldest reading first, config order among equals).
- **Lead protection** applies to automatic selection. An account named explicitly, or a resumed conversation that lives on the lead's account, is used regardless, with a warning when it would have been skipped.
- **Live queries are cached.** A reading is reused for `usage_cache_seconds`. A query is also skipped while a failed endpoint is in back-off, while another process is already querying the same account, and when less than a second of the time budget is left.

### When a dispatch counts as "would spend money"

An account is *paid* when a Codex reading shows a credit balance above zero, or a Claude reading shows paid overage enabled. A dispatch to a paid account is blocked unless spending is allowed (`QR_ALLOW_CREDITS=1` as a prefix of that command, or `general.allow_credits = true`) when any of these holds:

| Condition | Applies to |
|---|---|
| Weekly quota at 0% | both families |
| Five-hour window at 0% | Claude; Codex readings without the live `allowed` / `limit_reached` flags (the local fallback) |
| Live flags say `allowed = false` or `limit_reached = true` | Codex |
| Weekly quota (or the scarce model's bucket, for a dispatch that uses it) below `limits.weekly_floor`, or no weekly reading at all | both families |

A Claude dispatch without `--model` is judged against the scarce model's bucket as well, because the account's default model may be that model. This includes a resumed conversation without `--model`: the resume keeps its account as long as the shared quota has room, but it is blocked when the scarce bucket is exhausted or nearly so *and* the account has paid overage enabled.

The check passes, with a note, when the payment state is not known: an account directory that is not in the config file, a resumed conversation whose account cannot be found, a reading that carries no credit or overage information, or a per-model bucket the usage endpoint did not report. The hook also cannot stop a job that is already running from exhausting its quota and moving on to paid usage.

`qr pick` and `qr check` apply the same rules; they read `QR_ALLOW_CREDITS=1` from their environment. `qr pick --env` exits with status 3 and prints nothing on stdout when no account can take the dispatch. `--strict` is accepted for compatibility and has no effect.

### What the hook recognises as a dispatch

A `claude` with `-p` / `--print`, or a `codex` with `exec` / `e`, that stands where the shell would look for a command: at the start of a simple command, after variable assignments, after a shell keyword (`if`, `then`, `do`, `while`, `!`, `{` …), or after one of these launchers and their options: `env`, `nohup`, `time`, `timeout`, `gtimeout`, `noglob`, `command`, `exec`, `sudo`, `nice`, `caffeinate`, `stdbuf`, `setsid`, `xargs`, `parallel`, and `find … -exec` / `-execdir`. A `--` ends the options of those launchers.

Everything else is treated as data and left alone: text inside quotes, heredoc bodies (the delimiter may be any word, quoted or not), comments, and the arguments of any other program. That includes interpreters: in `python3 wrap.py -- claude -p hi` the hook cannot know whether `wrap.py` runs its arguments, so it does nothing. The executable may be spelled `claude`, `/path/to/claude`, `claude.exe`, `codex.cmd`, in any letter case for the suffixed forms.

An account is *named* by a `CLAUDE_CONFIG_DIR=…` / `CODEX_HOME=…` that is in effect for the dispatch: a prefix of the command, an `env` argument, or an earlier `export`. If the variable is cleared after that (`unset`, `env -i`, `env -u CODEX_HOME`), no account is named and the dispatch is routed normally. `env -u CLAUDE_CONFIG_DIR` in front of a `claude` dispatch is the exception: it is the spelling of the Claude account at the default location (`qr pick claude` prints it), so it names that account. Whether an `export` or `unset` behind `&&`, `||`, a pipe or inside `if` actually runs is not evaluated.

## Time limit

`timing.hook_budget_seconds` (default 8) runs from the moment the hook process enters its main function.

- **Cooperative deadline.** Every network exchange is bounded by what is left of the budget, including a response body that trickles in; each socket operation is additionally capped at 4 seconds. Lock waits are bounded by `min(2 s, time left)`. Local commands (`ps`, `security`, PowerShell) get `min(2 s, time left)`. Walking account directories for a resumed conversation and reading the local fallback logs check the clock per file and per line.
- **Parsing.** Reading the command text checks the same deadline. A command that cannot be read in time is not routed: the hook reports `Routing failed (BudgetTimeout)` and the command runs as written. If the budget was already used up when parsing starts, parsing gets a quarter of a second. Parsing is linear in the length of the command except for one case: many `claude` / `codex` words inside a single simple command cost time proportional to the square of their number (about one second for 20 000 of them).
- **After the decision.** Writing `decisions.log`, recording history, fetching reset notices and counting recent failures are each skipped when no time is left; a `skipped-…` line in `hook-errors.log` records it. A decision that used the whole budget therefore does not appear in `decisions.log`.
- **Watchdog.** A single read from a file system that has stopped answering, or a stdin that never closes, cannot be interrupted cooperatively. Two seconds after the budget has run out, a watchdog ends the hook process: nothing on stdout, `quota-router: time limit reached; command left unchanged` on stderr, exit status 0. Claude Code then runs the command as written. Reading stdin and the config file happens before the configured budget is known, so the default of 8 seconds applies to that part. The watchdog is cancelled once the decision is made, so an answer is never cut off halfway through being written.
- **Not covered.** The time Python needs to start and import the package (around 0.05 s on a laptop, more on a cold disk) comes before all of this. A background request that was abandoned at the deadline may live up to 4 more seconds in a daemon thread; it does not delay the process exit.

State files are written to a temporary file and renamed, so a hook ended by the watchdog cannot leave a half-written state file. It can leave a `*.tmp` file behind in the state directory.

## Log files

Both are in the state directory (`$QR_STATE`, else `~/.local/state/quota-router` or `$XDG_STATE_HOME/quota-router`; Windows `%LOCALAPPDATA%\quota-router`). They are always in English, whatever `general.language` says.

### `decisions.log`

One line per decision, nine fields separated by ` | `:

```
10-11 21:14:10 | hook | lead=claude-main | sess=3f9a1c2e | claude | auto | pick=claude-b | weekly 80% left; model other | cmd#5d41402abc4b
10-11 21:14:12 | hook | lead=claude-main | sess=3f9a1c2e | codex | deny | pick=- | blocked: weekly quota; model other | cmd#9b74c9897bac
```

| Field | Content |
|---|---|
| time | `MM-DD HH:MM:SS`, local time |
| source | `hook`, `qr pick`, `qr check` or `probe` |
| `lead=` | key of the account the dispatching session runs on, with ` (assumed)` when it was not detected; `?` when unknown |
| `sess=` | first 8 characters of Claude Code's session id; a hash when the id has an unusual shape; `-` when there is none |
| family | `claude`, `codex` or `?` |
| kind | `auto` (routed), `explicit` (account named in the command), `explicit-unknown` (named directory not in the config), `resume`, `resume-unknown`, `deny`, `fail`, `pick`, `check`, `probe` |
| `pick=` | account key; `-` when there is none |
| detail | `; `-separated: a fixed phrase, the numbers behind the choice (`weekly N% left`, `five-hour N% left`, `N running, N reserved`), `model <class>`, `credits allowed`. For `deny` only the category: `blocked: credits`, `paid overage`, `lead protection`, `five-hour window`, `weekly quota`, `wrong account for resume`, `no account available`, `other`. For `fail` the exception class name. |
| `cmd#` | first 12 hex digits of the SHA-256 of the command; `-` when there is no command |

Never written: the command or prompt, an email address, a directory, a note in the display language, an exception message. An account key, lead key, model class or session id that contains anything but letters, digits, `_`, `.` and `-` is written as `#` plus 12 hex digits of its hash. When several dispatches share one Bash command and one of them is denied, only the denial is logged.

### `hook-errors.log`

One line per event, five fields:

```
2026-10-11 21:02:02 | usage-query | ValueError | core.py:430:fetch | claude-b
2026-10-11 21:02:03 | lock-timeout | - | - | leases
2026-10-11 21:02:09 | skipped-history | - | - | -
```

Time, operation, exception class, code location (`file:line:function` of the innermost frame inside this package), subject (a state file, a lock name or an account key). Operations: `hook` (the hook itself failed; this is what "routing failed N times" counts), `usage-query`, `state-save`, `lock-timeout`, `lock-failed`, `unlock-failed`, `skipped-decision-log`, `skipped-history`, `skipped-notices`, `skipped-failure-count`.

No exception message and no traceback is written here, because both can quote request headers, response bodies and config values. With `QR_DEBUG=1`, full tracebacks are appended to `debug.log`. A pattern-based scrub is applied to that file (`Bearer …` / `Basic …` values, strings starting with `sk-`, JWT-shaped strings, `NAME=<20 or more characters>` unless the value looks like a path), but it is best effort.

### Other files in the state directory

| File | Content |
|---|---|
| `cache.json` | Last reading per account: usage windows, when fetched, account email, a short hash of the token (never the token), error text, back-off state |
| `leases.json` | Reservations of the last `lease_seconds`: account key, time, full session id |
| `installs.json` | What `qr install` put into each `settings.json` (below) |
| `failure-index.json` | Offsets into the two logs for counting recent failures |
| history, observation and notice files | Usage readings over time, detected reset events, which session has been told about which reset |
| `probe-<key>.log`, `probe-<key>.lock` | Output of the background Codex probe, and its once-per-interval marker |
| `*.lock` | Lock files; never hold data |

## `qr install` and ownership of the hook entry

`qr install [--config-dir DIR]` adds one matcher group to `hooks.PreToolUse` in that directory's `settings.json`:

```json
{"matcher": "Bash", "hooks": [{"type": "command", "command": "<absolute path of python>", "args": ["-I", "-X", "utf8", "<package dir>/__main__.py", "hook"]}]}
```

- Exec form: no shell is involved. `command` is the interpreter `qr install` itself ran on. `-I` keeps `PYTHON*` variables, the working directory and user site-packages from changing what is imported; `-X utf8` makes the pipes UTF-8 on Windows. The entry names the file rather than `-m quota_router` because `-I` hides a `pip install --user` or a source checkout from `-m`.
- A changed file is first copied to `settings.json.bak-<YYYYMMDD-HHMMSS>` and replaced in one step. Contents, key order, indent width, non-ASCII characters and the final newline are kept; objects written on one line or aligned by hand are re-wrapped as standard JSON.
- A `settings.json` that is not valid JSON, whose top level is not an object, or whose `hooks` / `hooks.PreToolUse` has another shape is left untouched; exit status 2.
- After writing, the installed command is run once with a test event. Exit status 1 when it does not start or does not answer. The test answer `{"quota_router": "ok", "config": "ok" | "missing" | "error"}` counts as answering even when the config file is missing or broken: a successful install says the hook runs, not that routing is configured. `qr doctor` reports the config separately.

**Which entries count as ours.** `qr install` records, per `settings.json`, the interpreter path and the complete argument list it wrote, in `installs.json`.

| Situation | An entry is ours when |
|---|---|
| A record exists for this `settings.json` | its `command` is the recorded interpreter and its `args` are exactly the recorded list; or it is exactly what this interpreter would write now |
| No record | its `args` are exactly `-I -X utf8 <this package's __main__.py> hook` and the file name of its `command` is a Python interpreter (`python`, `python3.12`, `python.exe`, `pypy3` …) |

Paths are compared as text; on Windows case and separators are folded. Anything else is not ours, in particular an entry that merely has the entry file among its arguments (`python -c "…" …/__main__.py hook`), has extra arguments, or runs it with a program that is not Python.

| State | Meaning | `qr install` | `qr uninstall` |
|---|---|---|---|
| ours, as it should be | | nothing (`unchanged`) | removed |
| ours, other interpreter or location, duplicated | `outdated` | updated in place, extra copies dropped | removed |
| ours, under a matcher that does not cover Bash | `misplaced` | moved to a `Bash` group; other hooks of that group stay | removed |
| `foreign`: an exec-form entry that runs some `quota_router/__main__.py` as `hook` but is not ours | another installation, or the record is gone and the package has moved | nothing changed, and nothing added (a second hook would route every command twice) | left alone |
| `manual`: a shell-form entry whose whole command is `qr hook`, optionally through Python or with leading assignments | hand-written | nothing changed | left alone |
| absent | | added | nothing |

A matcher covers Bash when it is missing, empty, `*`, `Bash`, or a pattern that fully matches `Bash` (`Bash|Write`). When removing an entry leaves a matcher group, `PreToolUse` or `hooks` empty, those are removed too; groups that were already empty are not touched.

## `qr doctor`

One line per check, starting with `OK` or `WARN`. It only looks: files are checked for existence, never read, and the keychain is not queried. **The exit status is always 0**, also with warnings; read the lines.

1. Python version (3.11 or newer) and interpreter path.
2. Platform.
3. Config file: loads, number of accounts; then one `WARN` per config warning. A missing or broken file is a `WARN`.
4. State directory can be created and written.
5. File locking works and is exclusive (a lock is taken and a second handle is checked to be refused).
6. The process list can be read.
7. Windows only: a reminder that the busy count is limited there, and whether `bash` is on `PATH`.
8. `claude` and `codex` are on `PATH` (each only when that family has accounts).
9. Per account: the directory exists; a login file exists (`.credentials.json` for Claude, `auth.json` for Codex). On macOS a Claude account without the file is reported as "login is normally in the keychain", which is an `OK` line and does not prove a login.
10. Per Claude account directory, and for the default one: the state of the hook entry in its `settings.json` (`installed`, `outdated`, `misplaced`, `manual`, `foreign`, `absent`, `unreadable`). `installed` and `manual` are `OK`.
11. For every entry found: the listed command is run once with the test event. `WARN` when it cannot be started, times out after 20 seconds or does not answer, with up to three lines of its stderr. A shell-form entry is run through `/bin/sh -c` (Windows: `bash` from `PATH`).

## Windows

| Feature | Behaviour on Windows | Limits |
|---|---|---|
| Config file | `%APPDATA%\quota-router\config.toml` | |
| State directory | `%LOCALAPPDATA%\quota-router` | `XDG_STATE_HOME` is not consulted |
| Claude login | Read from `<dir>\.credentials.json`; no command is run | A Console sign-in under `%APPDATA%\Anthropic` is not read. Whether Claude Code can store its login in Credential Manager is not known; if it does, it is not read. |
| Codex login | Read from `<dir>\auth.json` | Not readable when Codex is set to keep its login in the system credential store; the numbers then come from the local logs |
| File lock | `msvcrt.locking` on byte 0 of a dedicated `.lock` file | A mandatory lock, so it is never taken on a data file |
| Replacing a state file | `os.replace`, retried three times 20 ms apart when another process has the target open | Still failing: one error line, and the result is used from memory for this run |
| Query timeout | Worker thread, as elsewhere | An abandoned request lives up to 4 more seconds |
| Process list | PowerShell `Get-CimInstance Win32_Process`, filtered to command lines containing `claude` or `codex`; 2-second timeout; cached for 5 seconds within one process | Starting PowerShell takes roughly 0.5 to 2 seconds of the hook's budget (an estimate, not measured). On a machine where it times out, the busy count is always 0 and only reservations spread the load. |
| Another process's environment | Not readable | |
| Busy count | A job is attributed to an account only when `CLAUDE_CONFIG_DIR=` / `CODEX_HOME=` is visible on its own command line or that of up to 8 parents (`env.exe VAR=… claude -p …`, `bash.exe -c "VAR=… codex exec …"`) | Jobs that got their account from `export` or inheritance, jobs at the default location with nothing written, and Claude jobs the hook routed to the default account (it writes `env -u CLAUDE_CONFIG_DIR`, which names no directory) are not counted for any account. Reservations of the last five minutes still are. |
| Background probe | `DETACHED_PROCESS \| CREATE_NEW_PROCESS_GROUP`; the executable is looked up with `shutil.which` first | An npm-installed `codex` is a `.cmd` file; argument quoting through `.cmd` has not been tested on a real machine |
| Paths written into commands | Forward slashes, quoted when needed: `env CLAUDE_CONFIG_DIR=C:/Users/me/.claude-b claude -p …` | Assumes Git Bash and both CLIs accept forward slashes in these variables |
| Paths in your commands and in the config file | `C:\…`, `C:/…`, `~/…` and `/c/…` all match the account, case-insensitively | In Git Bash a backslash path must be quoted; that is the shell's rule |
| Finding a resumed conversation | `<dir>\projects\<slug>\*.jsonl`, where the slug replaces every character that is not a letter or digit with `-` | The slug rule was observed on macOS and carried over; it has not been compared with what Claude Code produces on Windows |
| Shell | Only commands of the Bash tool (Git Bash) | Commands run through Claude Code's PowerShell tool are never routed |
| Console output | The hook's stdout/stderr are UTF-8; other `qr` commands keep the console encoding and replace what it cannot show with `?` | On a legacy code page, emoji and Chinese text in `qr status` may show as `?` |

On macOS and Linux the busy count reads each job's environment from `ps`. A directory with a space in its name cannot be told apart there from two separate words, so jobs on such an account are not counted either; reservations still are.

See [research-windows.md](research-windows.md) for the sources these choices are based on.

## Tests

`python -m pytest` runs everything against a throwaway home directory. The network, keychain, process table and background probe are replaced by fakes, and a test that reaches a real one fails. A few tests start loopback HTTP servers on `127.0.0.1`.

`tests/specs/` holds 17 behaviour specs as standalone scripts; `tests/test_specs.py` runs each in its own interpreter and pins the number of checks (1379 in total), so none can disappear unnoticed. They assert on the Traditional Chinese messages.

### Skipped tests

| Where | What | Why |
|---|---|---|
| Windows | `tests/specs/test_b1_state.py` | Imports `fcntl` and takes `flock` directly to hold the reservation lock |
| Windows | `tests/specs/test_b3_history.py` | Takes `fcntl.flock` directly to hold the observation lock |
| Windows | `tests/specs/test_fix_regressions.py` | Asserts on `signal.getitimer` and runs commands through `/bin/zsh` |
| Windows | Nine cases in `tests/test_review_fixes.py` | Run the generated command through `/bin/sh` |
| Windows | `tests/test_fresh_machine.py` | The virtual-environment layout and the fake `security` / `ps` programs are POSIX. The same steps are covered piecewise by `test_install.py` and `test_hook.py`. |
| Windows | One test in `tests/test_install.py` | Runs a hand-written shell-form entry through `/bin/sh` |
| macOS, Linux | One test in `tests/test_final_review.py` | Needs Windows path rules (a drive path is absolute only there) |
| Anywhere without a cached `setuptools` and `uv` | `tests/test_fresh_machine.py` | It installs into a new virtual environment without the network. Set `QR_E2E_ONLINE=1` to let it download instead; CI does. |

A test in `tests/test_specs.py` scans the spec sources and fails if the Windows skip list and the files that actually use `fcntl`, `getitimer` or `/bin/zsh` ever differ. There is no expected-failure list on any platform. On Linux the specs run with the platform pinned to the macOS branch, because the operating-system layer is faked in them with macOS-shaped data; each platform's own code is tested in `tests/test_platform_compat.py`.

### What the fresh-machine test does and does not show

It creates a virtual environment, installs the package from a copy of the source, runs `qr init`, `qr install`, `qr doctor`, feeds the installed hook command a real `PreToolUse` event against a local stand-in for the usage endpoints, checks the rewritten command and the logs, and uninstalls. It launches the hook command itself, exactly as written into `settings.json`. It does not run Claude Code, so it does not show that a given Claude Code version accepts the exec-form entry (`command` plus `args`).
