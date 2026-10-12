# quota-router reference

Everything the [README](../README.md) leaves out: configuration, the full selection rules, every message, known limitations, platform differences, privacy, uninstalling, and development. The README's terms apply here too: the **lead** is the Claude Code session that issues the command, a **dispatch** is a `claude -p` / `codex exec` command the hook sees, and a **family** is Claude or Codex.

`qr --help` lists the commands and `qr config` prints every setting with its current value and source; those two outputs are the source of truth for command syntax and resolved values.

## Configuration

### Config file

Location: `$QR_CONFIG` if set, otherwise `~/.config/quota-router/config.toml` (Windows: `%APPDATA%\quota-router\config.toml`). Only `[[accounts]]` is required.

```toml
[general]
language = "en"

[[accounts]]
family = "claude"
key = "claude-main"
dir = ""                   # empty = the CLI's default location

[[accounts]]
family = "claude"
key = "claude-b"
dir = "~/.claude-b"

[[accounts]]
family = "codex"
key = "codex-1"
dir = ""

[[accounts]]
family = "codex"
key = "codex-2"
dir = "~/.codex-b"
```

### Accounts

| Field | Default | Meaning |
|---|---|---|
| `family` | required | `claude` or `codex`. |
| `key` | required | A unique name of your choosing (letters, digits, `-`, `_`, `.`). It appears in messages and logs. |
| `dir` | `""` | The directory you would pass as `CLAUDE_CONFIG_DIR` / `CODEX_HOME`. `~` and `$HOME` are expanded. Empty, or equal to `~/.claude` / `~/.codex`, means the CLI's default location; one account per family may use it. |

The order of the accounts matters in three cases: when two accounts score exactly the same, the one listed first is used; when two Claude directories are the same account, the first one that is not the lead's represents it; and when the time budget cannot cover every account, accounts with equally old readings are queried in this order (the oldest reading first, config order among equals).

Two Claude directories logged in to the same account (same email) are treated as one account: one quota, busy counts added together.

A family with no accounts listed is not managed at all. Its dispatches are neither routed nor checked, so it has no credit protection either. With only Claude accounts listed, `codex` commands are left untouched.

Each account directory is a separate CLI configuration: models, permissions, instructions and integrations are whatever that directory holds. quota-router does not synchronize them, so a routed job runs with the settings of the account it lands on.

In messages an account is shown by the part of its login email before the `@`, or by its config key where the email is not known or a list of accounts is given.

#### `dir`

- `~` and `$HOME` (any environment variable, in fact) are expanded when the file is loaded.
- **Windows:** `C:\…`, `C:/…`, `~/…` and the Git Bash spelling `/c/…` are all accepted. `/c/…` is converted to `C:\…` when the file is loaded; this conversion happens on Windows only, since on other systems `/c/…` is an ordinary path.
- A Claude account at the default location is run with `CLAUDE_CONFIG_DIR` *unset* (`env -u CLAUDE_CONFIG_DIR claude …`), not with the variable pointing at `~/.claude`. A Codex account always gets `CODEX_HOME=<dir>`.

### Settings

| Setting | Default | Meaning |
|---|---|---|
| `general.language` | `"en"` | Message language, `en` or `zh-TW`. `QR_LANG` overrides it. |
| `general.protect_threshold` | `20` | The lead's own account is not picked for subagents once its tightest window (weekly or five-hour) is below this %. It can still be named explicitly, and a conversation that lives there can still be resumed. |
| `general.allow_credits` | `false` | `true` lets every dispatch spend purchased credits / paid overage without the `QR_ALLOW_CREDITS=1` prefix. |
| `hints.home_low` | `30` | Start looking at the other family when this family's best weekly % is below this… |
| `hints.other_high` | `50` | …and suggest it when the other family's best weekly % is at least this, or at least double. |
| `hints.other_idle` | `90` | Also suggest an idle account of the other family with at least this weekly %… |
| `hints.home_busy` | `3` | …when the account picked here already has this many jobs, or less than half that quota. |
| `limits.five_hour_floor` | `10` | An account is skipped when its five-hour window is below this %. |
| `limits.weekly_floor` | `10` | A non-lead account is skipped when its weekly quota is below this %. |
| `limits.tight_warning` | `15` | Warn that all accounts are low when the best weekly % is below this. |
| `timing.usage_cache_seconds` | `180` | How long a usage reading counts as fresh. |
| `timing.profile_cache_seconds` | `86400` | How long an account's email is cached. |
| `timing.lease_seconds` | `1800` | A reservation made by a dispatch expires after this. |
| `timing.busy_window_seconds` | `300` | A reservation counts as "just dispatched" (busy) for this long. |
| `timing.hook_budget_seconds` | `8` | Time the hook may spend on one command. Two seconds past it, the hook process is ended and the command runs unchanged. See [Time limit](#time-limit). |
| `timing.codex_stale_hours` | `2` | Codex numbers that are not live and older than this are treated as unknown. |
| `timing.codex_probe_hours` | `2` | Minimum gap between background Codex probes per account. |
| `timing.notice_ttl_hours` | `6` | A reset notice is shown for this long. |
| `events.reset_drop_points` | `3` | Usage must drop by this many points to count as a reset or refill. |
| `families.claude.scarce_model` | `"fable"` | The Claude model that has its own weekly bucket. `""` turns the per-model check off. |
| `endpoints.claude_usage` | `"https://api.anthropic.com/api/oauth/usage"` | Where Claude usage is queried. |
| `endpoints.claude_profile` | `"https://api.anthropic.com/api/oauth/profile"` | Where a Claude account's email is queried. |
| `endpoints.codex_usage` | `"https://chatgpt.com/backend-api/wham/usage"` | Where Codex usage is queried. |

`[endpoints]` exists for tests and for networks that reach the vendors through a proxy. **Your login token is sent to whatever these point at**; keep the defaults unless you need one of those two cases. `qr init` writes them commented out, so that a later change of the default addresses is not pinned by old config files.

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

### Environment variables

| Variable | Effect |
|---|---|
| `QR_CONFIG` | Path of the config file. |
| `QR_STATE` | State directory. Default `~/.local/state/quota-router` (or under `$XDG_STATE_HOME`); Windows `%LOCALAPPDATA%\quota-router`. |
| `QR_LANG` | `en` or `zh-TW`; overrides the config file. |
| `QR_ALLOW_CREDITS=1` | As a prefix of one dispatch (`QR_ALLOW_CREDITS=1 codex exec …`), lets that dispatch spend credits. `export` does not count for the hook; `qr pick` / `qr check` read it from the environment. |
| `QR_NO_PROBE=1` | Never start the background Codex probe (see [Privacy and security](#privacy-and-security)). |
| `QR_DEBUG=1` | Also write full tracebacks to `debug.log` in the state directory. That file can contain sensitive text. |
| `QR_E2E_ONLINE=1` | Tests only: lets `tests/test_fresh_machine.py` download packages (see [Development](#development)). |

### Constants that are not settings

Tolerance when comparing reset dates: 60 seconds. History file size cap: 2 MB. Observation index retention: 30 days. Back-off after a failed usage query: 60 s doubling to 15 minutes (a `Retry-After` header wins). Score scaling for a tight five-hour window starts under 30%. The "about to expire" hint needs at least 30% left and a reset within 24 hours.

## Install notes

What each README install step does, and what it does not prove.

- **Installing the package.** Until it is on PyPI, install from GitHub. On a Python that refuses to install packages directly (Homebrew's on macOS, Debian's, most distro Pythons; the error says "externally-managed-environment"), or when there is no `pip` command, use a virtual environment:

  ```sh
  python3 -m venv ~/.qr-venv
  ~/.qr-venv/bin/pip install git+https://github.com/nikeru8/quota-router
  ~/.qr-venv/bin/qr --help
  ```

  Then either call `~/.qr-venv/bin/qr` by that path or add `~/.qr-venv/bin` to your `PATH`. `pipx install git+https://github.com/nikeru8/quota-router` does the same in one step if you have pipx. `qr` does not have to be on Claude Code's `PATH`: the hook is installed with the absolute path of the Python it runs on.
- **Running from a checkout without installing:** `git clone`, then `bin/qr <command>` (`python bin\qr <command>` on Windows).
- **Claude Code version.** `qr install` writes the hook in exec form (`command` plus `args`). No minimum Claude Code version has been established; step 5 of the install is the check.
- **Step 1, `qr init`.** It pre-fills the default location of each CLI plus every `~/.claude-*` and `~/.codex-*` directory that looks like an account. It only checks which files exist and reads none of them, so **an account in the generated file is not necessarily logged in**: the two default locations are always listed, even on a machine that has never used that CLI. The exact rules are under [`qr init`](#qr-init).
- **Step 2.** Delete every account that `qr status` shows without a reading because nobody is logged in there.
- **Step 3, `qr install`.** Each Claude account directory has its own `settings.json`, so run it once per account you dispatch from. It adds one `PreToolUse` entry for `Bash` and leaves the rest of the file as it was. The previous file is saved next to it as `settings.json.bak-<time>`. Running it again changes nothing. It then runs the installed command once with a harmless test event and exits with status 1 if that command does not answer. A successful install means the hook starts; it does not mean routing is configured, since the hook also answers the test without a config file. Details: [`qr install` and ownership of the hook entry](#qr-install-and-ownership-of-the-hook-entry).
- **Step 4, `qr doctor`.** It exits with status 0 even when it prints warnings; read the lines. What it checks: [`qr doctor`](#qr-doctor).

## Commands

```
qr init [--force]                    write an example config
qr config                            print the resolved settings and their sources
qr install [--config-dir DIR]        add the hook to settings.json (default ~/.claude)
qr uninstall [--config-dir DIR]      remove the hook that `qr install` added
qr doctor                            one OK/WARN line per check of the setup
qr status                            table of every account
qr pick <family>                     print the best account as an env prefix and reserve it
                                     (--env, --peek, --model <name>; --strict is accepted and ignored)
qr check <family> <key|number|dir>   check one specific account
qr report [--days N]                 reset events, current pace, dispatch statistics (default 7 days)
qr decisions [N]                     the last N routing decisions (default 30)
qr leases --clear                    drop all reservations
qr hook                              the Claude Code PreToolUse hook (reads the hook JSON on stdin)
```

`qr status` and the summary line show the age of the numbers. "live" means read within the last few minutes, not necessarily in this very call.

`qr check codex <number>` means "the n-th Codex account in the config file", so it changes when you reorder the file. Use keys in scripts.

### Using `qr pick` in scripts

The hook reads the text of the Bash command and nothing else. A dispatch that happens inside a script the command runs is not in that text, so the script has to ask for an account itself. `qr pick` exits with status 0 and prints the prefix, or exits with status 3, prints nothing on stdout and explains on stderr. `qr pick` and `qr check` apply the same rules as the hook, including the credit check; they return an account that would spend credits only when `QR_ALLOW_CREDITS=1` is in their environment.

```sh
account=$(qr pick codex --env) || exit 3
eval "env $account codex exec \"\$prompt\""
```

The `eval` is there because the directory is shell-quoted when it contains a space or another special character.

### `qr init`

Lists the default location of each family first, whether or not anything exists there, then every `~/.claude-*` and `~/.codex-*` directory that contains one of these marker files or directories: `.claude.json`, `.credentials.json`, `settings.json`, `projects` (Claude); `auth.json`, `config.toml`, `sessions` (Codex). It checks that they exist and reads none of them. Keys: the directory name without the dot for Claude (`.claude-b` → `claude-b`), `codex-2`, `codex-3`, … for Codex; a key that would collide or hold an invalid character is adjusted (`claude-main-2`). `[endpoints]` is written commented out.

### `qr install` and ownership of the hook entry

`qr install [--config-dir DIR]` adds one matcher group to `hooks.PreToolUse` in that directory's `settings.json`:

```json
{"matcher": "Bash", "hooks": [{"type": "command", "command": "<absolute path of python>", "args": ["-I", "-X", "utf8", "<package dir>/__main__.py", "hook"]}]}
```

- Exec form: no shell is involved. `command` is the interpreter `qr install` itself ran on. `-I` keeps `PYTHON*` variables, the working directory and user site-packages from changing what is imported; `-X utf8` makes the pipes UTF-8 on Windows. The entry names the file rather than `-m quota_router` because `-I` hides a `pip install --user` or a source checkout from `-m`.
- A changed file is first copied to `settings.json.bak-<YYYYMMDD-HHMMSS>` and replaced in one step. Contents, key order, indent width, non-ASCII characters and the final newline are kept; objects written on one line or aligned by hand are re-wrapped as standard JSON. The backup is the untouched original.
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

### `qr doctor`

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

## How it decides

- **Same family only.** Claude or Codex is your choice. The hook picks the account and never switches family.
- **Readings.** Each account's quota is queried live and the answer is reused for `usage_cache_seconds`. No query is made while an endpoint that just failed is in back-off, while another hook process is already querying the same account, or when less than a second of the time budget is left; the last known numbers are used then.
- **Score.** Weekly % left divided by hours until the weekly reset, so quota that is about to expire is used first. A five-hour window under 30% scales the score down. The result is divided by 1 + the number of jobs on that account: running `claude -p` / `codex exec` processes or dispatches made in the last `busy_window_seconds` (five minutes), whichever is larger.
- **Order of preference.** Accounts that would not spend money come before accounts that would (only relevant once spending is allowed), whatever their score; then the higher score; then the earlier position in the config file.
- **Accounts that are skipped.** Five-hour window under `five_hour_floor`; weekly quota used up; for accounts other than the lead's, weekly quota under `weekly_floor`; not logged in, meaning the directory holds no credentials and there is no earlier reading (`not logged in`). Naming a not-logged-in account explicitly still runs the command as written, with a warning.
- **Unknown readings stay in.** An account whose quota cannot be read (network failure, expired token, no local numbers) is not skipped. It gets the median score of the accounts that are known, and the note says the quota is unknown. So an account you listed but cannot be queried can still receive a job. The lead's own account is the exception: unknown means protected.
- **Old numbers are used rather than nothing.** When every account is blocked, readings that are not fresh are queried once more. If an account's numbers are still old after that, it is not the lead's and it could not cost money, the job is sent there with a `⚠` warning that the numbers are old: quota may have reset since.
- **The lead is protected.** The account the lead runs on is not picked once its tightest window is below `protect_threshold`, or when its quota cannot be read. This applies to automatic selection: naming that account yourself, or resuming a conversation that lives on it, still uses it, with a warning when it would have been skipped.
- **Nothing fits: block.** The whole Bash command is refused, with the reason for each account, the earliest scheduled reset and, when it could be read, the state of the other family.
- **Money is off by default.** A dispatch that the readings say would draw on purchased Codex credits or Claude paid overage is always blocked: on automatic routing, on an account you named, on a resumed conversation, and in `qr pick` / `qr check`. Spending is allowed only by `QR_ALLOW_CREDITS=1` written as a prefix of that command (`export` does not count for the hook), or for everything by `general.allow_credits = true`. Those are the only two ways through. The exact conditions are in [When a dispatch counts as "would spend money"](#when-a-dispatch-counts-as-would-spend-money).
- **Resumed conversations stay put.** `claude -p … --resume <id>` and `codex exec resume <id>` go to the account that holds the conversation. If it is out of quota, the command is blocked and you are told to dispatch again elsewhere with a summary.
- **You named the account: it is used.** `CLAUDE_CONFIG_DIR=… claude -p …` and `CODEX_HOME=… codex exec …` run as written, with a warning if that account would have been skipped. See [What the hook recognises as a dispatch](#what-the-hook-recognises-as-a-dispatch) for how a named account is detected.
- **The other family is a suggestion.** A dispatch gets a one-line summary of the other family when that family has accounts and could be read in time, and a 💡 line when it has clearly more room (thresholds: the `hints.*` settings). A command with several dispatches gets the summary for its first one. The lead decides.
- **One model can have its own bucket.** With `scarce_model` set, a Claude dispatch that names that model, or names none, is also checked against that model's weekly bucket. A resumed conversation without `--model` keeps its account as long as the shared quota has room, but is checked against that bucket for paid overage. If the usage endpoint does not report the bucket, there is nothing to check.
- **Resets are read, not assumed.** When a live reading shows usage dropping by at least `reset_drop_points`, it is recorded as a reset or refill and the lead is told once per session. An ordinary five-hour rollover on schedule is not announced, and a reset date that merely moved is recorded for `qr report` without a notice.
- **Fail open.** If quota-router itself breaks, the command runs as written. With a missing or broken config file the hook also lets every command through unchanged. The hook aims to answer within `hook_budget_seconds`; two seconds after that a watchdog ends it without a decision. The time Python takes to start is on top. See [Time limit](#time-limit).

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

### Time limit

`timing.hook_budget_seconds` (default 8) runs from the moment the hook process enters its main function.

- **Cooperative deadline.** Every network exchange is bounded by what is left of the budget, including a response body that trickles in; each socket operation is additionally capped at 4 seconds. Lock waits are bounded by `min(2 s, time left)`. Local commands (`ps`, `security`, PowerShell) get `min(2 s, time left)`. Walking account directories for a resumed conversation and reading the local fallback logs check the clock per file and per line.
- **Parsing.** Reading the command text checks the same deadline. A command that cannot be read in time is not routed: the hook reports `Routing failed (BudgetTimeout)` and the command runs as written. If the budget was already used up when parsing starts, parsing gets a quarter of a second. Parsing is linear in the length of the command except for one case: many `claude` / `codex` words inside a single simple command cost time proportional to the square of their number (about one second for 20 000 of them).
- **After the decision.** Writing `decisions.log`, recording history, fetching reset notices and counting recent failures are each skipped when no time is left; a `skipped-…` line in `hook-errors.log` records it. A decision that used the whole budget therefore does not appear in `decisions.log`.
- **Watchdog.** A single read from a file system that has stopped answering, or a stdin that never closes, cannot be interrupted cooperatively. Two seconds after the budget has run out, a watchdog ends the hook process: nothing on stdout, `quota-router: time limit reached; command left unchanged` on stderr, exit status 0. Claude Code then runs the command as written. Reading stdin and the config file happens before the configured budget is known, so the default of 8 seconds applies to that part. The watchdog is cancelled once the decision is made, so an answer is never cut off halfway through being written.
- **Not covered.** The time Python needs to start and import the package (around 0.05 s on a laptop, more on a cold disk) comes before all of this. A background request that was abandoned at the deadline may live up to 4 more seconds in a daemon thread; it does not delay the process exit.

State files are written to a temporary file and renamed, so a hook ended by the watchdog cannot leave a half-written state file. It can leave a `*.tmp` file behind in the state directory.

## Messages you will see

All of them go to the lead session, attached to the Bash tool call. They are shown in English by default and in Traditional Chinese with `general.language = "zh-TW"` (or `QR_LANG=zh-TW`); the examples below are the English ones. `alice`, `bob`, `carol`, `dave` and the account keys are made up.

Routed:

```
[quota-router] Automatically routed to bob: weekly 88% left (resets in 4.0 d), 0 jobs running, 1 recently reserved
```

The other family, when it is configured and could be read:

```
Codex accounts: carol 70% (resets in 4.0 d, scheduled), 0 running / dave 55% (resets in 4.0 d, scheduled), 0 running (reading age: live)
```

No `--model` on a Claude dispatch:

```
No --model specified; checking quota as if this command uses Fable
```

The other family has more room:

```
💡 Claude's best account has only 25% left, while carol on Codex still has 70%. Unless this task needs Claude for quality reasons, consider rewriting it as codex exec.
```

Everything is low, but the dispatch went through:

```
⚠ All accounts are low on quota. Earliest scheduled reset: codex-1, shared weekly quota, at 10-15 21:34. The account may still be unavailable then.
```

You named an account that would have been skipped:

```
[quota-router] Running on bob as you specified, although: Only 3% of weekly quota remains, below the 10% minimum. The account with the most available quota is alice: weekly 41% left (resets in 4.0 d)
```

A resumed conversation:

```
[quota-router] Resuming: keeping the original account bob.
```

Blocked, no account available:

```
Dispatch #1 (claude/other) was blocked; the whole command was not run.
No account can take this dispatch
claude-main: lead account has only 15% left; reserved for the lead
claude-b: weekly quota used up
Earliest scheduled reset: claude-b, shared weekly quota, at 10-15 21:33. The account may still be unavailable then.
Codex accounts: carol 70% (resets in 4.0 d, scheduled), 0 running / dave 55% (resets in 4.0 d, scheduled), 0 running (reading age: live)
To force it, name the account explicitly (not possible when credits would be charged). If work is still pending, dispatch again later; the tool re-checks every time.
```

Blocked because it would cost money. Here both Codex accounts are out of subscription quota and one of them has purchased credits:

```
Dispatch #1 (codex/other) was blocked; the whole command was not run.
No account can take this dispatch
codex-1: weekly quota used up
codex-2: subscription quota used up; will draw on purchased credits; credit balance 40
Earliest scheduled reset: codex-1, shared weekly quota, at 10-15 21:34. The account may still be unavailable then.
Once the user agrees, write QR_ALLOW_CREDITS=1 as a prefix of that one command (for example QR_ALLOW_CREDITS=1 codex exec …; export does not count).
Claude accounts: alice 15% (resets in 4.0 d, scheduled), 0 running / bob 0% (resets in 4.0 d, scheduled), 0 running (reading age: live)
To force it, name the account explicitly (not possible when credits would be charged). If work is still pending, dispatch again later; the tool re-checks every time.
Only the user can authorize spending credits for this task. Ask first; do not enable it yourself.
```

A quota window came back:

```
🔄 bob: shared weekly reset (rolled over on schedule, due 10-15 21:33): 4% → 100% left, some time between 10-15 20:10 and 10-15 21:40. More non-Fable dispatches can go to this account; Fable is still limited by its own bucket, and the five-hour window and lead protection still apply.
```

quota-router itself failed:

```
[quota-router] Routing failed (OSError); running the command as written.
```

With a missing or broken config file nothing is attached at all; the command runs unchanged and the hook writes one line to stderr, for example `quota-router: no config at …, run "qr init"`.

## Known limitations

The rule behind all of these: **what the hook cannot see, it does not route, and it does not check for credits either.** It reads the text of a Bash command before the command runs. It does not run a shell, expand variables or look inside scripts. A dispatch counts only when `claude` / `codex` stands where the shell looks for a command, possibly behind a known launcher (full list in [What the hook recognises as a dispatch](#what-the-hook-recognises-as-a-dispatch)).

| You write | What happens | Write this instead |
|---|---|---|
| `claude -p x --resume=ID` (equals sign) | Treated as a new dispatch. It may be moved to another account, where the conversation does not exist. | A space: `--resume ID` |
| `codex exec <flags> resume ID` (flags in between) | Treated as a new dispatch; may be moved to another account. | Name the original account yourself: `CODEX_HOME=… codex exec …` |
| `claude -p x -c`, `codex exec resume --last` (no id) | The account is guessed from the most recent conversation file, which may be the lead's own. | `--resume <id>` / `resume <id>` |
| `bash -c '… claude -p …'`, `eval` | Not seen: no routing, no notes. | Call `qr pick <family> --env` inside the string |
| `python3 wrap.py -- claude -p x`, `./run.sh claude -p x` (arguments of another program) | Not seen: the hook cannot know whether that program runs its arguments. | Call `qr pick` in the wrapper, or write the account yourself |
| `"/opt/My Tools/claude" -p x` (quoted executable) | Not seen. | An unquoted path, or `claude` on `PATH` |
| `CLAUDE -p x` (upper case without `.exe` / `.cmd`) | Not seen. | `claude` |
| `claude --resume ID`, `claude -c` (no `-p`) | Not a dispatch: an interactive session is never routed. | Add `-p` for a one-shot job |
| `claude -p x -- --help` | `--help` anywhere after the program name makes the hook step aside, also after `--`. | Keep `--help` / `--version` out of prompts passed as bare words; quote the prompt |
| `codex exec resume --model m ID` (option before the id) | The option's value is taken for the conversation id. | Put the id first: `codex exec resume ID --model m` |
| `false && export CODEX_HOME=…; codex exec x`, `export` inside `if` | Whether an `export` or `unset` really runs is not evaluated. The hook may take the account as named when it is not, or the other way round. | Write the account as a prefix of the dispatch itself |
| `>log claude -p x` (redirection first) | Not seen: no routing. | Put the redirection after the command |
| `for … do claude -p …; done`, `xargs` | Seen as one dispatch: the whole batch lands on one account and is counted once. | Call `qr pick` inside the loop |
| `$(brew --prefix)/bin/claude -p x` | The prefix is inserted twice and the command breaks. | Write `claude` or an absolute path |

Also:

- Only Claude Code's Bash tool is hooked. Subagents started with the built-in Agent/Task tool or a workflow run on the lead's own account. A dispatch made inside a script is not in the command text, so it is neither routed nor checked (use `qr pick` in the script, see [Using `qr pick` in scripts](#using-qr-pick-in-scripts)).
- Text that is only data is left alone: quoted strings, comments and heredoc bodies, whatever the delimiter. A dispatch written inside a heredoc that is fed to a shell (`bash <<EOF … EOF`) is therefore not seen either.
- A Bash command is allowed or blocked as a whole. In `codex exec a; claude -p b`, a blocked Claude dispatch stops the Codex part too.
- Only the weekly window, the five-hour window and one per-model weekly bucket are modelled.
- `qr install` rewrites `settings.json` as standard JSON. Contents, key order and indentation width are kept; hand-aligned or single-line objects are re-wrapped. The backup is the untouched original.
- An account whose directory name contains a space is not credited with its running jobs on macOS and Linux (the process environment cannot be split reliably there); reservations still count.

## Platform notes

| | macOS | Linux | Windows |
|---|---|---|---|
| Config file | `~/.config/quota-router/config.toml` | same | `%APPDATA%\quota-router\config.toml` |
| State directory | `~/.local/state/quota-router` (or `$XDG_STATE_HOME`) | same | `%LOCALAPPDATA%\quota-router` |
| Claude login read from | keychain, else `<dir>/.credentials.json` | `<dir>/.credentials.json` | `<dir>\.credentials.json` |
| Codex login read from | `<dir>/auth.json` | same | same |
| Running jobs counted from | `ps`, including each job's environment | same | PowerShell `Get-CimInstance Win32_Process`, command lines only |
| Shell the command runs in | `sh` / `zsh` | `sh` / `bash` | Git Bash |

The test suite runs on all three in CI. Day-to-day use so far has been on macOS; treat Windows as the least proven.

- **macOS.** The first keychain read may bring up a system permission prompt.
- **macOS and Linux.** The busy count reads each job's environment from `ps`. A directory with a space in its name cannot be told apart there from two separate words, so jobs on such an account are not counted; reservations still are.
- **Windows: Git for Windows is required.** Claude Code's Bash tool runs in Git Bash, and Bash commands are all this hook sees. Commands that Claude Code runs through its PowerShell tool are never routed.
- **Windows: the busy count is weaker.** Windows does not let one process read another's environment, so a running job is only attributed to an account when `CLAUDE_CONFIG_DIR=…` / `CODEX_HOME=…` is visible on its command line or its parent's. Jobs the hook routed to a named directory qualify. A job that got its account from an earlier `export` does not, and neither does a Claude job routed to the default account, because that one is written as `env -u CLAUDE_CONFIG_DIR` and names no directory. Reservations from the last five minutes are counted the same on every platform.
- **Windows: listing processes starts PowerShell**, which takes roughly 0.5–2 s of the hook's budget. Past 2 s nobody is counted as busy.
- **Windows: paths** are written into commands with forward slashes (`C:/Users/me/.claude-b`). In your own commands and in the config file, `C:\…`, `C:/…`, `~/…` and the Git Bash spelling `/c/…` are all understood; the config loader converts `/c/…` to a drive path on Windows.
- **Windows: logins kept elsewhere are not read.** That includes a Console sign-in under `%APPDATA%\Anthropic` and a Codex login kept in the system credential store.

### Windows details

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

See [research-windows.md](research-windows.md) for the sources these choices are based on.

## Privacy and security

**What it reads**, all read-only:

- The Claude Code OAuth token: on macOS from the login keychain through `security find-generic-password`, otherwise from `<dir>/.credentials.json`.
- The Codex token in `<dir>/auth.json`.
- As fallbacks when a query fails: the usage Claude Code cached in `.claude.json`, and the last `rate_limits` record in Codex's `<dir>/sessions/**.jsonl`. To find that record up to 20 of the most recent session files are read line by line; only the record is kept.
- The names and modification times of conversation files in the account directories, to find which account holds a resumed conversation.
- The process list, to count running `claude` / `codex` jobs per account. That means the command lines of running processes, which can contain prompt text, and on macOS and Linux the environment of the `claude` / `codex` ones. Only the program name and `CLAUDE_CONFIG_DIR` / `CODEX_HOME` are used; none of it is stored.

quota-router itself never refreshes, rewrites or copies a token.

**Where it sends anything.** Each token goes to its own vendor's usage endpoint and nowhere else. HTTP redirects are never followed; a redirect counts as a failed query:

- `https://api.anthropic.com/api/oauth/usage`
- `https://api.anthropic.com/api/oauth/profile`
- `https://chatgpt.com/backend-api/wham/usage`

quota-router itself sends no telemetry and makes no other request, unless you change `[endpoints]`.

**The background Codex probe.** When a live Codex query fails and the local numbers are over two hours old, one `codex exec` no-op ("Reply with the single word: ok", read-only sandbox, low reasoning effort) is started in the background to refresh them. That is the real Codex CLI running on that account: it talks to its vendor, may refresh its own login and writes a session file, all outside the guarantees above. It uses the model that account's Codex config selects, so what it costs depends on that config; it is small but not zero. It is not started when the account is known to have credits and its subscription quota is used up, nearly used up or unknown; when the credit balance itself is unknown, it is. It happens at most once per account per `codex_probe_hours` (two hours), and `QR_NO_PROBE=1` turns it off.

**What it writes**, all in the state directory:

- State files: a usage cache, reservations, usage and reset history, and a record of what `qr install` installed. These hold account emails, short hashes of tokens (never the tokens) and the session ids of recent reservations.
- `decisions.log`: one line per decision with the time, the lead's account key, the first 8 characters of the session id (or a hash of it), the chosen account key, family, kind of decision, quota numbers, model class and a 12-character hash of the command. No command text, no prompt, no email, no directory.
- `hook-errors.log`: one line per error with the time, operation name, exception class, code location and the account key or state file concerned. No exception message and no traceback, because those can quote headers and response bodies.
- `probe-<account>.log`: whatever the `codex` CLI printed during a background probe.
- `*.lock` files and, briefly, `*.tmp` files.
- `debug.log`, **only with `QR_DEBUG=1`**: full tracebacks. A pattern-based scrub is applied, but it is best effort; this file can contain tokens, headers, response bodies and config values. Treat it as sensitive and delete it when you are done.

The field-by-field formats are under [Log files](#log-files).

**The hook rewrites commands.** That is its job, and it is also the risk. A program that runs before every Bash command and can change it is a privileged thing to install. What it does is narrow: it inserts an environment prefix in front of a `claude` or `codex` invocation, or blocks the whole command; it changes nothing else in the command. A bug could still send a job to the wrong account or break a command it misreads (see [Known limitations](#known-limitations)). Read `qr decisions` after your first few dispatches. Anyone who can edit the config file can add accounts or redirect `[endpoints]`, and with them your tokens, so protect it like `settings.json`.

### Log files

Both are in the state directory (`$QR_STATE`, else `~/.local/state/quota-router` or `$XDG_STATE_HOME/quota-router`; Windows `%LOCALAPPDATA%\quota-router`). They are always in English, whatever `general.language` says.

#### `decisions.log`

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

#### `hook-errors.log`

One line per event, five fields:

```
2026-10-11 21:02:02 | usage-query | ValueError | core.py:430:fetch | claude-b
2026-10-11 21:02:03 | lock-timeout | - | - | leases
2026-10-11 21:02:09 | skipped-history | - | - | -
```

Time, operation, exception class, code location (`file:line:function` of the innermost frame inside this package), subject (a state file, a lock name or an account key). Operations: `hook` (the hook itself failed; this is what "routing failed N times" counts), `usage-query`, `state-save`, `lock-timeout`, `lock-failed`, `unlock-failed`, `skipped-decision-log`, `skipped-history`, `skipped-notices`, `skipped-failure-count`.

No exception message and no traceback is written here, because both can quote request headers, response bodies and config values. With `QR_DEBUG=1`, full tracebacks are appended to `debug.log`. A pattern-based scrub is applied to that file (`Bearer …` / `Basic …` values, strings starting with `sk-`, JWT-shaped strings, `NAME=<20 or more characters>` unless the value looks like a path), but it is best effort.

#### Other files in the state directory

| File | Content |
|---|---|
| `cache.json` | Last reading per account: usage windows, when fetched, account email, a short hash of the token (never the token), error text, back-off state |
| `leases.json` | Reservations of the last `lease_seconds`: account key, time, full session id |
| `installs.json` | What `qr install` put into each `settings.json` (see [`qr install` and ownership of the hook entry](#qr-install-and-ownership-of-the-hook-entry)) |
| `failure-index.json` | Offsets into the two logs for counting recent failures |
| history, observation and notice files | Usage readings over time, detected reset events, which session has been told about which reset |
| `probe-<key>.log`, `probe-<key>.lock` | Output of the background Codex probe, and its once-per-interval marker |
| `*.lock` | Lock files; never hold data |

## Uninstall

```sh
qr uninstall                              # and once per --config-dir you installed into
pip uninstall quota-router
```

`qr uninstall` removes the entry `qr install` wrote, recognised by its exact command and argument list, and nothing else; entries written by hand, by another copy, or that merely mention this package are left alone. The rules are in [`qr install` and ownership of the hook entry](#qr-install-and-ownership-of-the-hook-entry). The config file and the state directory stay behind for you to delete.

## FAQ

**Why route subagents at all?**
- *Several accounts, one session.* A subagent runs on whichever account its environment points at. Left alone, they all pile onto the account the lead uses.
- *Weekly quota expires.* Each account's allowance resets on its own schedule and whatever is unused is gone. The account that resets soonest should be used first.
- *The lead does not look.* It cannot see quota unless something shows it, and it will not check before every dispatch.
- *Running out can cost money without a word.* An account with purchased credits or paid overage enabled keeps working after the subscription quota is gone; it just starts charging.

**Why doesn't it switch between Claude and Codex automatically?**
The two CLIs take different flags, and the same prompt gets you different work. Which one a task needs is a judgement about quality that only the lead session, or you, can make. So the hook reports the other family's quota alongside a dispatch, says so when it has clearly more room, and leaves the command alone.

**Why are paid credits blocked by default?**
Because the failure is silent. An account with credits keeps working after its subscription quota is gone and bills you for it, and an agent has no reason to notice. Spending money should be something you agreed to for that task. Putting `QR_ALLOW_CREDITS=1` in front of one command makes the consent explicit, limited to that command and visible in the transcript. `allow_credits = true` in the config removes the check if you do not want it. Those are the only two ways through.

**Why should Claude dispatches spell out `--model`?**
Without it the CLI uses the account's default model, which may be the one with its own, smaller weekly bucket. The hook cannot tell, so it assumes the scarce model and checks that bucket: a dispatch can then be blocked, or sent to a worse account, even though a cheaper model would have been fine. With `--model opus` (or any other name) it is scored against the right quota, and your subagents stop spending the scarce bucket by accident.

**Does it work without Codex?**
Yes. List only Claude accounts; `codex` commands are then left untouched.

## Development

```sh
pip install -e . pytest
python -m pytest
```

To run from a checkout without installing: `bin/qr <command>` (`python bin\qr <command>` on Windows). [CHANGELOG.md](../CHANGELOG.md) says what each version contains.

### Tests

`python -m pytest` runs everything against a throwaway home directory. The network, keychain, process table and background probe are replaced by fakes, and a test that reaches a real one fails; the tests never read real account data. A few tests start loopback HTTP servers on `127.0.0.1`.

`tests/specs/` holds 17 behaviour specs as standalone scripts; `tests/test_specs.py` runs each in its own interpreter and pins the number of checks (1379 in total), so none can disappear unnoticed. They assert on the Traditional Chinese messages.

#### Skipped tests

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

#### What the fresh-machine test does and does not show

It creates a virtual environment, installs the package from a copy of the source, runs `qr init`, `qr install`, `qr doctor`, feeds the installed hook command a real `PreToolUse` event against a local stand-in for the usage endpoints, checks the rewritten command and the logs, and uninstalls. It launches the hook command itself, exactly as written into `settings.json`. It does not run Claude Code, so it does not show that a given Claude Code version accepts the exec-form entry (`command` plus `args`).
