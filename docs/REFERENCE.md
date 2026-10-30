# quota-router reference

Everything the [README](../README.md) leaves out: configuration, the full selection rules, the `claude` launcher, every message, known limitations, platform differences, privacy, uninstalling, and development. The README's terms apply here too: the **lead** is the Claude Code session that issues the command, a **dispatch** is a `claude -p` / `codex exec` command the hook sees, and a **family** is Claude or Codex.

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
| `family` | required | `claude`, `codex`, `agy` (Antigravity CLI), `kimi` (Kimi Code CLI), or `grok` (Grok Build CLI). |
| `key` | required | A unique name of your choosing (letters, digits, `-`, `_`, `.`). It appears in messages and logs. |
| `dir` | `""` | The directory you would pass as `CLAUDE_CONFIG_DIR` / `CODEX_HOME`. `~` and `$HOME` are expanded. Empty, or equal to `~/.claude` / `~/.codex`, means the CLI's default location; one account per family may use it. Exception, Claude on macOS: `dir = "~/.claude"` written out is a second, separate account (see [macOS: two logins in `~/.claude`](#macos-two-logins-in-claude)). |
| `launch` | `true` | Claude accounts only. `false`: [`qr launch`](#sessions-and-the-claude-launcher) never starts a new session here. Even with `true`, a session only starts here once the hook is in this directory's `settings.json`. Dispatches from the hook still go to the account either way, and a conversation that lives there is still resumed there. |

The order of the accounts matters in three cases: when two accounts score exactly the same, the one listed first is used; when two Claude directories are the same account, the first one that is not the lead's represents it; and when the time budget cannot cover every account, accounts with equally old readings are queried in this order (the oldest reading first, config order among equals).

Two Claude directories logged in to the same account (same email) are treated as one account: one quota, busy counts added together.

A family with no accounts listed is not managed at all. Its dispatches are neither routed nor checked, so it has no credit protection either. With only Claude accounts listed, `codex` commands are left untouched.

Each account directory is a separate CLI configuration: `settings.json` (with the hook, permissions, MCP servers, model, output style), `CLAUDE.md`, plugins and skills are whatever that directory holds. quota-router does not copy or synchronize any of it, so a routed job or a new session runs with the settings of the account it lands on. See [Which accounts are set up for sessions](#which-accounts-are-set-up-for-sessions).

In messages an account is shown by the part of its login email before the `@`, or by its config key where the email is not known or a list of accounts is given.

#### `dir`

- `~` and `$HOME` (any environment variable, in fact) are expanded when the file is loaded.
- **Windows:** `C:\…`, `C:/…`, `~/…` and the Git Bash spelling `/c/…` are all accepted. `/c/…` is converted to `C:\…` when the file is loaded; this conversion happens on Windows only, since on other systems `/c/…` is an ordinary path.
- A Claude account at the default location is run with `CLAUDE_CONFIG_DIR` *unset* (`env -u CLAUDE_CONFIG_DIR claude …`), not with the variable pointing at `~/.claude`. A Codex account always gets `CODEX_HOME=<dir>`.

### macOS: two logins in `~/.claude`

On macOS Claude Code keeps each login in the keychain under an item named after the value of `CLAUDE_CONFIG_DIR`: one item for a plain `claude` (the variable unset) and a different one for `CLAUDE_CONFIG_DIR=/Users/you/.claude claude`, even though both are the same directory. So the default location can hold two accounts at once. quota-router models that: `dir = ""` is the login a plain `claude` uses and is dispatched to with `env -u CLAUDE_CONFIG_DIR`; `dir = "~/.claude"` written out is the other login and is dispatched to with `CLAUDE_CONFIG_DIR=/Users/you/.claude`. Both may be in the config file; when they turn out to be the same account, the usual merging by e-mail applies. `qr doctor` warns when the second item holds a login that is not in the config. On Linux and Windows the two spellings read the same `.credentials.json`, so there `dir = "~/.claude"` still means the default location.

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
| `families.agy.default_model` | `""` | The `--model` added to an `agy -p` dispatch that names none, e.g. `"gemini-4-argon-high"`. `""` adds nothing. |
| `families.claude.refresh_logins` | `false` | `true`: renew an expired Claude login with its refresh token, in a background process, and save it where Claude Code keeps it, so an idle account keeps live numbers. `false` (or `QR_NO_REFRESH=1`): never; an idle account then shows "login token expired" until Claude Code is used there. See [Renewing an expired Claude login](#renewing-an-expired-claude-login). |
| `families.kimi.default_model` | `"kimi-code/k3.1-pro-highspeed"` | Model added to `kimi -p` / `--prompt` when `-m` / `--model` is absent; empty disables injection. |
| `families.kimi.refresh_logins` | `true` | Renew expiring Kimi credentials; `QR_NO_REFRESH=1` disables this. |
| `families.grok.default_model` | `""` | Model added to `grok -p` / `--single` when `-m` / `--model` is absent, e.g. `"grok-4.7"`; empty adds nothing. |
| `families.grok.refresh_logins` | `false` | `true`: when a Grok login has expired, run `grok models` in the background so the CLI renews it itself; `QR_NO_REFRESH=1` disables this. See [Grok Build family](#grok-build-family). |
| `families.codex.refresh_logins` | `false` | `true`: renew a Codex login with its refresh token before its access token expires, in a background process, and save it into that account's `auth.json`. `false` (or `QR_NO_REFRESH=1`): never. See [Renewing a Codex login](#renewing-a-codex-login). |
| `families.codex.refresh_before_hours` | `72` | A Codex login is renewed once its access token (about ten days) expires within this many hours. |
| `notify.on_refresh_failed` | `""` | A shell command run when a login renewal (either family) fails or finds its refresh token dead. `""`: none. See [When a renewal fails](#when-a-renewal-fails). |
| `notify.repeat_hours` | `12` | The same account and reason runs that command at most once in this many hours. |
| `notify.failed_attempts` | `2` | A temporary failure (`failed`) runs that command only after this many in a row; a dead login, an unsaved renewal and an account on hold run it at once. A renewal that succeeds starts the count again. |
| `launch.route_print` | `false` | `true`: the [launcher](#sessions-and-the-claude-launcher) also chooses the account for `claude -p …` started outside Claude Code. By default those run as written. |
| `endpoints.claude_usage` | `"https://api.anthropic.com/api/oauth/usage"` | Where Claude usage is queried. |
| `endpoints.claude_profile` | `"https://api.anthropic.com/api/oauth/profile"` | Where a Claude account's email is queried. |
| `endpoints.claude_token` | `"https://platform.claude.com/v1/oauth/token"` | Where an expired Claude login is renewed. The refresh token is sent here. |
| `endpoints.codex_usage` | `"https://chatgpt.com/backend-api/wham/usage"` | Where Codex usage is queried. |
| `endpoints.codex_token` | `"https://auth.openai.com/oauth/token"` | Where a Codex login is renewed. The refresh token is sent here. |

Kimi endpoint overrides are an exception: only approved Kimi HTTPS hosts are accepted, never arbitrary proxies. See [Kimi Code family](#kimi-code-family). So is `endpoints.grok_billing`: only `https://cli-chat-proxy.grok.com`. See [Grok Build family](#grok-build-family).

`[endpoints]` exists for tests and for networks that reach the vendors through a proxy. **Your login token, and for `claude_token` / `codex_token` the refresh token, is sent to whatever these point at**; keep the defaults unless you need one of those two cases. `qr init` writes them commented out, so that a later change of the default addresses is not pinned by old config files.

### Validation

| Input | Result |
|---|---|
| Unknown top-level section, unknown setting, unknown field in an `[[accounts]]` table, unknown table under `[families]` | Warning; the value is ignored. Warnings are printed by `qr config` (stderr) and `qr doctor` only. The hook and the other commands load the file silently. |
| Wrong type (number where a string is expected and so on), negative number, `inf` or `nan` | Error. |
| `launch` in an `[[accounts]]` table, or `launch.route_print`, that is not `true` / `false` | Error. |
| `launch` on a Codex account | Warning; it only applies to Claude accounts and is ignored. |
| `general.language` other than `en` / `zh-TW` | Error. |
| `endpoints.*` not starting with `http://` or `https://` | Error. |
| No `[[accounts]]`, an account without `family` or `key`, `family` other than `claude` / `codex` / `agy` / `kimi` / `grok`, a key with characters outside letters, digits, `-`, `_`, `.`, a duplicate key | Error. |
| `dir` that is not absolute after expansion | Error. |
| Two accounts of one family at the default location (`dir = ""`, or a `dir` that resolves to `~/.claude` / `~/.codex`; on macOS a Claude `dir = "~/.claude"` is a separate account instead) | Error. |
| Two accounts of one family with the same directory | Error. |
| File is not UTF-8 or not valid TOML | Error. |

On an error, `qr status`, `qr pick`, `qr check`, `qr report`, `qr decisions`, `qr leases` and `qr config` print one line and exit with status 2. `qr doctor` reports the error as a `WARN` line and still exits 0. `qr install` and `qr uninstall` do not need a usable config at all. The hook prints one line on stderr and lets the command run unchanged. `qr launch` prints one line on stderr and starts claude on the default account.

### Environment variables

| Variable | Effect |
|---|---|
| `QR_CONFIG` | Path of the config file. |
| `QR_STATE` | State directory. Default `~/.local/state/quota-router` (or under `$XDG_STATE_HOME`); Windows `%LOCALAPPDATA%\quota-router`. |
| `QR_LANG` | `en` or `zh-TW`; overrides the config file. |
| `QR_ALLOW_CREDITS=1` | As a prefix of one dispatch (`QR_ALLOW_CREDITS=1 codex exec …`), lets that dispatch spend credits. `export` does not count for the hook; `qr pick` / `qr check` read it from the environment. |
| `QR_NO_PROBE=1` | Never start the background Codex probe (see [Privacy and security](#privacy-and-security)). |
| `QR_NO_REFRESH=1` | Never renew a Claude or Codex login, whatever `refresh_logins` says (see [Renewing an expired Claude login](#renewing-an-expired-claude-login) and [Renewing a Codex login](#renewing-a-codex-login)). |
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

  Without `--config-dir` it also writes the `claude` launcher to `~/.local/share/quota-router/bin/claude` and, when that directory is not yet ahead of the real `claude` on your `PATH`, prints the line to add to `~/.zshrc` or `~/.bashrc`. It never edits those files itself. `--no-launcher` installs the hook only; `--config-dir` never touches the launcher. New sessions only land on account directories that have the hook, so run the `--config-dir` form for every account you want sessions on. Details: [The launcher file](#the-launcher-file).
- **Step 4, `qr doctor`.** It exits with status 0 even when it prints warnings; read the lines. What it checks: [`qr doctor`](#qr-doctor).
- **Step 6, the launcher on `PATH`.** With the launcher's directory first on `PATH`, `claude` in a new terminal prints one line before the session starts, when stderr is a terminal: `[quota-router] session on <account>: …` for a routed session, or a line saying why it started on the default account. `qr doctor` runs `claude --version` through the launcher and checks its place on `PATH`.

## Commands

```
qr init [--force]                    write an example config
qr config                            print the resolved settings and their sources
qr install [--config-dir DIR] [--no-launcher]
                                     add the hook to settings.json (default ~/.claude) and write the claude launcher
qr uninstall [--config-dir DIR] [--no-launcher]
                                     remove the hook and the launcher that `qr install` added
qr launch [--] [claude arguments]    start claude on the Claude account with the most quota left (what the launcher runs)
qr doctor                            one OK/WARN line per check of the setup
qr status                            table of every account
qr status --bars                     the table with a bar per quota window under each account
qr status --watch [N]                the table with bars, redrawn every N seconds (default 10) until Ctrl-C
qr watch [N]                         the same, shorter (also: qr status watch [N], qr status bars)
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

`--env` prints only the prefix: `CODEX_HOME=<dir>`, `CLAUDE_CONFIG_DIR=<dir>`, or `env -u CLAUDE_CONFIG_DIR` for the Claude account at the default location. The directory is shell-quoted when it contains a space or another special character, hence the `eval`. The account is reserved as a dispatch of the hook would be. Run outside Claude Code with the `claude` launcher first on `PATH`, `env -u CLAUDE_CONFIG_DIR claude -p …` passes through the launcher unchanged (unless `[launch] route_print = true`), so the account `qr pick` chose is kept.

### `qr init`

Lists the default location of each family first, whether or not anything exists there, then every `~/.claude-*`, `~/.codex-*`, `~/.kimi-code-*`, and `~/.grok-*` directory that contains one of these marker files or directories: `.claude.json`, `.credentials.json`, `settings.json`, `projects` (Claude); `auth.json`, `config.toml`, `sessions` (Codex); `credentials/kimi-code.json`, `config.toml`, `sessions` (Kimi); `auth.json`, `config.toml`, `sessions` (Grok). It checks that they exist and reads none of them. Keys: the directory name without the dot for Claude (`.claude-b` → `claude-b`), `codex-2`, `codex-3`, … for Codex, `kimi-2`, `kimi-3`, … for Kimi, `grok-2`, … for Grok; a key that would collide or hold an invalid character is adjusted (`claude-main-2`). `[endpoints]` is written commented out.

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

One line per check, starting with `OK` or `WARN`. It only looks: credential files are checked for existence, never read, and the keychain is not queried. It does read `settings.json`, the launcher file, `installs.json`, `cache.json` and the shell startup files named below. **The exit status is always 0**, also with warnings; read the lines.

1. Python version (3.11 or newer) and interpreter path.
2. Platform.
3. Config file: loads, number of accounts; then one `WARN` per config warning. A missing or broken file is a `WARN`.
4. State directory can be created and written.
5. File locking works and is exclusive (a lock is taken and a second handle is checked to be refused).
6. The process list can be read.
7. Windows only: a reminder that the busy count is limited there, and whether `bash` is on `PATH`.
8. `claude` and `codex` are on `PATH` (each only when that family has accounts). When the `claude` found first is the launcher, the real one behind it is reported.
9. Per account: the directory exists; a login file exists (`.credentials.json` for Claude, `auth.json` for Codex). On macOS a Claude account without the file is reported as "login is normally in the keychain", which is an `OK` line and does not prove a login.
10. Per Claude account directory, and for the default one: the state of the hook entry in its `settings.json` (`installed`, `outdated`, `misplaced`, `manual`, `foreign`, `absent`, `unreadable`). `installed` and `manual` are `OK`.
11. For every entry found: the listed command is run once with the test event. `WARN` when it cannot be started, times out after 20 seconds or does not answer, with up to three lines of its stderr. A shell-form entry is run through `/bin/sh -c` (Windows: `bash` from `PATH`).
12. The launcher: present (`WARN` when absent; the rest of the launcher checks are then skipped), written by this installation, executable; and it is run once as `claude --version` (which passes straight through to the real claude), `WARN` when that exits non-zero, prints nothing or takes more than 20 seconds, with up to three lines of its stderr.
13. Its directory comes before the real claude on `PATH`; otherwise `WARN` with the line to add.
14. Every alias or function named `claude` in `~/.zshrc`, `~/.zprofile`, `~/.zshenv` (under `$ZDOTDIR` when set), `~/.bashrc`, `~/.bash_profile`, `~/.bash_aliases` and `~/.profile`, with file and line number. `OK` for an alias whose command is `claude` (optionally after `command`, `env` or variable assignments), since the shell then finds the launcher on `PATH`; `WARN` for an alias that runs anything else (an absolute path, `npx`, …), one that sets `CLAUDE_CONFIG_DIR`, and for a function. The line is shown with credential-looking text masked and cut at 200 characters. No alias or function: one `OK` line.
15. With a usable config that has Claude accounts: one `WARN` per account with `launch` not `false` but no hook in its `settings.json`, with the `qr install --config-dir …` to run; at least one account set up for sessions (`WARN` otherwise); and at least one of those has a reading in `cache.json` (`WARN`: run `qr status`).

Not checked yet (see "Known issues" in [CHANGELOG.md](../CHANGELOG.md)): a `CLAUDE_CONFIG_DIR` exported in a shell startup file or already in `qr doctor`'s environment, with which every `claude` passes through the launcher unrouted; and a `claude` alias or function in a file that the listed startup files `source`.

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
- **agy is a family of its own.** An `[[accounts]]` entry with `family = "agy"` (one only, `dir` empty: agy has no directory variable) makes `agy -p` / `--print` / `--prompt` a dispatch. Its quota is read by running `agy -p /usage`, which takes 15–60 s or more, so never inside a reading: when the cached numbers are due, a reading starts one background process (`refresh-agy <account>`, output in `refresh-<account>.log` in the state directory, up to two minutes) and returns the cached numbers at once; the next reading has the new ones. They are due after 30 minutes while the lower of the Gemini weekly and five-hour windows has 50 % or more left, after 10 minutes from 20 %, after `usage_cache_seconds` below that or before the first reading, and when a window's reset time has passed. No second query starts while one runs or during a failure cooldown. Gemini models are checked against the Gemini bucket, `--model claude-…` / `gpt-…` against agy's Claude and GPT bucket. Nothing is inserted to select an account; a dispatch without `--model` gets `families.agy.default_model`. Interactive `agy` and `agy -i` are sessions and are left alone. agy appears in the other families' summary and hint lines, and they in its.
- **Resets are read, not assumed.** When a live reading shows usage dropping by at least `reset_drop_points`, it is recorded as a reset or refill and the lead is told once per session. An ordinary five-hour rollover on schedule is not announced, and a reset date that merely moved is recorded for `qr report` without a notice.
- **Fail open.** If quota-router itself breaks, the command runs as written. With a missing or broken config file the hook also lets every command through unchanged. The hook aims to answer within `hook_budget_seconds`; two seconds after that a watchdog ends it without a decision. The time Python takes to start is on top. See [Time limit](#time-limit).

The hook routes dispatches only, and it does not count interactive sessions as busy. New sessions are chosen by the launcher, with the differences listed in [Sessions and the claude launcher](#sessions-and-the-claude-launcher).

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

## Sessions and the claude launcher

The hook only sees the `claude -p` / `codex exec` commands a session runs through its Bash tool. Everything a session does in its own process (the built-in Agent tool, workflows) runs on the account the session was started on. The launcher chooses that account when the session starts.

`qr install` writes a small script named `claude` into `~/.local/share/quota-router/bin/` (`$XDG_DATA_HOME/quota-router/bin/` when that is set; Windows: `%LOCALAPPDATA%\quota-router\bin\claude.cmd`). Put that directory first on `PATH`, in `~/.zshrc` or `~/.bashrc`:

```sh
export PATH="$HOME/.local/share/quota-router/bin:$PATH"
```

From then on `claude` in a terminal asks `qr launch` which account to use, then starts the real `claude` with that account's `CLAUDE_CONFIG_DIR` (or with it unset, for the account at the default location). The real `claude` is looked up on `PATH` at every start, past the launcher's directory, so updating or moving it needs nothing. When stderr is a terminal, one line says where the session went (examples under [Messages you will see](#messages-you-will-see)). `qr launch [--] <claude arguments>` does the same without the launcher. Its decisions are in `decisions.log` with the source `qr launch`.

### How the launcher chooses

- **A new session** goes to the best account that is set up for sessions, by the hook's rules: the same score (weekly % left per hour to the reset, scaled for a tight five-hour window and for jobs running), the same skips (five-hour floor, weekly floor, weekly quota used up, not logged in, paid overage unless allowed), and a reservation as with `qr pick`. Three differences from a dispatch: there is no lead yet, so no account is held back as the lead's; interactive sessions already running on an account count as jobs there; and an account whose quota can be read comes before one whose quota cannot. Without `--model`, the scarce model's bucket counts too, as for a dispatch.
- **A resumed conversation** (`--resume <id>`, `-r <id>`, `--resume=<id>`) starts on the account that holds it. `--continue` / `-c`, `--resume` without an id (the picker) and `--from-pr` go to the account of the last session started through the launcher in this directory; without one, to the newest conversation of this directory that is not a `claude -p` run. This also holds for an account that is not set up for sessions or has no quota left, with a `⚠` in the line. When the account cannot be found, the session starts on the default account. Details: [`--continue`: what it can and cannot know](#--continue-what-it-can-and-cannot-know).
- **Left alone, nothing printed:** `CLAUDE_CONFIG_DIR` already set (you, or the hook, chose); a command run inside a Claude Code session (`CLAUDECODE` set: the hook handles those); `claude -p …` (scripts and `qr pick` write the default account as `env -u CLAUDE_CONFIG_DIR claude -p …`; set `[launch] route_print = true` to route one-shot runs started outside Claude Code as well); `--help`, `-h`, `--version`, `-v`, `--bg`; a subcommand: `mcp`, `config`, `doctor`, `update`, `install`, `auth`, `plugin`, `setup-token`, `migrate-installer`, and any other bare lower-case word in first position. So `claude hello` starts unrouted, `claude "hello there"` is routed. The full rules are under [Arguments](#arguments).
- **It never refuses.** When every account is blocked, no account's quota can be read, the config file is missing or broken, quota-router fails, or choosing takes more than `hook_budget_seconds` plus two seconds, the session starts on the default account and the line says why. An account that would bill paid overage is never chosen automatically. When a session lands on one anyway (a resumed conversation, the default account as fallback, or with `QR_ALLOW_CREDITS=1` / `allow_credits = true`), the line says `⚠ PAID OVERAGE`.
- **It never stands in the way.** When quota-router itself cannot run (the Python it was installed with is gone, the package was removed or is an older version without `qr launch`, or it fails), the launcher starts the first other `claude` on `PATH` unchanged. `qr doctor` runs `claude --version` through the launcher to check that it starts claude.

What it does not do:

- **It does not move a running session. One session = one account.** Subagents from the Agent tool and workflows stay on the session's account however low it gets; start a new session to use another one.
- It does not edit `~/.zshrc` or any other startup file. `qr doctor` points out a `claude` alias or function that gets in the way. An alias that runs `claude` itself (`alias claude='claude --dangerously-skip-permissions'`) still goes through the launcher, because the shell looks the `claude` inside an alias up on `PATH`. An alias that runs an absolute path, or that sets `CLAUDE_CONFIG_DIR`, bypasses it.
- It is not free: every `claude` started this way first starts Python (about a tenth of a second), and reading quota that is not fresh can take up to `hook_budget_seconds`. Dispatches inside a session also pass through the launcher when it is first on that session's `PATH`; it steps aside at once.

### The launcher file

`qr install` without `--config-dir` (and unless `--no-launcher`) writes `<data dir>/quota-router/bin/claude`, where the data directory is `$XDG_DATA_HOME` or `~/.local/share`; on Windows `%LOCALAPPDATA%\quota-router\bin\claude.cmd`. `qr install --config-dir` and `qr uninstall --config-dir` never touch it: one launcher serves every account. On macOS and Linux it is an `sh` script, mode 755, that

1. checks that the recorded interpreter is executable and the entry file exists, then runs `<python> -I -X utf8 <package dir>/__main__.py launch --resolve -- "$@"`;
2. reads its answer, four lines: `quota-router-launch 1`, `keep` / `unset` / `set`, the directory, the real claude; sets or removes `CLAUDE_CONFIG_DIR` accordingly and `exec`s that claude, so claude takes over the process, its terminal and its signals;
3. if any of that fails (interpreter or entry file gone, an older package without `qr launch`, which prints its usage and exits with status 2, an import error, any other non-zero exit, an answer it does not recognise), looks through `PATH` itself and `exec`s the first `claude` that is not in its own directory and does not carry the marker `quota-router launcher` in its first 512 bytes, with the arguments unchanged; when there is none, one line on stderr and exit status 127.

So the launcher is never the reason `claude` does not start. The interpreter and entry file are the ones the hook entry uses, for the same reason: `-I` keeps the environment and working directory from changing what is imported, and naming the file (not `-m quota_router`) still works where `-I` hides a `pip install --user` or a source checkout.

The Windows file is a batch file: when the interpreter, the entry file and `launch.py` next to it exist, it runs `"<python>" -I -X utf8 "<entry>" launch -- %*` and returns its exit status (Python then starts claude as a child); otherwise it runs the first `claude` that `where claude` lists outside its own directory. It does not fall back when Python starts but fails. Untested on a real machine.

`qr install` never edits a shell startup file. When the directory is not on `PATH` ahead of the real claude, it prints `export PATH="$HOME/.local/share/quota-router/bin:$PATH"` (the directory quoted when it is not under the home directory or holds unusual characters; on Windows the directory itself).

**Which file counts as ours.** `installs.json` records, under the launcher's path, the interpreter, the entry file and the SHA-256 of the exact bytes written.

| State of the file | `qr install` | `qr uninstall` |
|---|---|---|
| absent | written | nothing (a stale record is dropped) |
| exactly what this interpreter writes | nothing (`already in place`); rewritten when it lost its executable bit | removed |
| exactly what an earlier `qr install` recorded (another interpreter or location) | rewritten | removed |
| anything else: written by hand, by another installation without a record here, or edited after install | left alone, with a message | left alone, with a message |

The directory itself is never removed: it may be on `PATH`.

### Which claude is started

The first file named `claude` (on Windows: `claude` plus each `PATHEXT` suffix, in that order) on `PATH` that is executable, is not in the launcher's directory, and does not carry the marker `quota-router launcher` in its first 512 bytes (a launcher written for another interpreter or data directory). Empty and relative `PATH` entries are skipped, so a `claude` in the working directory is never started. It is looked up at every start. None found: `qr launch` prints one line and exits with status 127 (with `--resolve` it exits with status 1 and the launcher reports it).

### Arguments

The words before `--` are read; after `--` everything is the prompt. The list of options that take a value was taken from `claude --help` of claude 2.1.296 and lives in one place, `VALUE_FLAGS` in `quota_router/launch.py`; an option claude adds later and that takes a value may make its value look like a subcommand, and that session then starts unrouted.

| Words | Effect |
|---|---|
| `--help`, `-h`, `--version`, `-v`, `--bg` | pass through (background sessions are kept per config directory, and `claude agents` / `attach` / `logs` / `stop` look in the default one) |
| `-p`, `--print`, or short options written together that contain `p` (`-pc`) | pass through, unless `[launch] route_print = true` |
| first word that is not an option and not an option's value, before any `-p` / `--print`: a known subcommand (`mcp`, `config`, `doctor`, `update`, `install`, `auth`, `plugin`, `setup-token`, `migrate-installer`, `daemon`, `agents`) or any word made of lower-case letters, digits and `-` starting with a letter | pass through (an unknown subcommand is never taken for a prompt; a one-word lower-case prompt is passed through as well) |
| `--resume ID`, `-r ID`, `--resume=ID` | resume `ID` |
| `--resume` / `-r` not followed by a word, `--resume=`, `--continue`, `-c`, short options written together that contain `c`, `--from-pr N` | resume as `--continue` (below) |
| `--model NAME`, `--model=NAME` | score for that model; without it, the scarce model is assumed, as for a dispatch |
| options that take a value (`--add-dir`, `--agent(s)`, `--allowedTools` / `--allowed-tools`, `--append-system-prompt(-file)`, `--autocompact`, `--betas`, `--debug-file`, `--disallowedTools` / `--disallowed-tools`, `--effort`, `--fallback-model`, `--file`, `--input-format`, `--json-schema`, `--max-budget-usd`, `--max-turns`, `--mcp-config`, `-n` / `--name`, `--output-format`, `--permission-mode`, `--permission-prompt-tool`, `--permission-prompts`, `--plugin-dir`, `--plugin-url`, `--session-id`, `--setting-sources`, `--settings`, `--system-prompt(-file)`, `--tools`, `-w` / `--worktree`) | their value is skipped |

Before the arguments are read: `CLAUDECODE` set (a command inside a Claude Code session, where the hook decides) passes through. After them: `CLAUDE_CONFIG_DIR` present in the environment (even empty) passes through; when that start is a session, it is recorded for `--continue` (below). A config file without Claude accounts passes through as well: that family is not managed.

### Which accounts are set up for sessions

Each account directory carries its own settings: `settings.json` (with the hook, permissions, MCP servers, model, output style), `CLAUDE.md`, plugins and skills. quota-router does not copy or synchronise any of it. A new session therefore only lands on a Claude account whose `launch` is not `false` **and** whose `settings.json` has a quota-router hook that Bash reaches (`installed_state` is `installed`, `outdated`, `manual` or `foreign`; not `absent`, `misplaced` or `unreadable`). A directory without the hook would also leave the session's own dispatches unrouted. `qr config` marks such an account `(launch: hook missing)`, `qr status` lists them under the Claude table, and `qr doctor` warns with the `qr install --config-dir …` to run. A resumed conversation and the default-account fallback ignore this: the conversation exists only where it is, and the default account is what `claude` would have used anyway.

### `--continue`: what it can and cannot know

Claude Code chooses the session id after it starts, so the launcher cannot record which conversation a session will write. It records, in `launched.json` in the state directory, every session it starts (and every session started with `CLAUDE_CONFIG_DIR` written out): a 16-hex-digit hash of the working directory, the account key and the time; 30 days, at most 500 entries. `--continue` (and `--resume` without an id, `--from-pr`) then goes to:

1. the account of the most recent launch in this directory that wrote a conversation there: a conversation file of this directory (`<dir>/projects/<slug>/*.jsonl`) on that account that is not a one-shot or subagent transcript (below) and was changed no earlier than five seconds before the launch. A launch the user quit without typing anything therefore does not count, however many older dispatch transcripts that account holds;
2. otherwise the account with the newest such file, leaving out files whose first records say they come from a one-shot or SDK run (`"entrypoint": "sdk-…"`) or a subagent (`"isSidechain": true`), and files newer than the latest launch here on another account (most likely that session's dispatches);
3. otherwise the default account, with a line saying the conversation was not found.

Only facts are recorded: a new session (routed, or on the default account as fallback), a session started with `CLAUDE_CONFIG_DIR` written out, and `--resume <id>` when the id was found on that account. The account `--continue`, a bare `--resume` or `--from-pr` inferred is not recorded, so a wrong guess is not taken as the answer the next time. Recording a start with `CLAUDE_CONFIG_DIR` written out waits at most one second, lock included, and never delays that start longer.

It cannot know about sessions started without the launcher and without `CLAUDE_CONFIG_DIR` (for example with an alias to an absolute path, or before the launcher was installed): a launch record wins over a newer conversation that such a session wrote on another account. It does not read the conversations beyond their first lines, and the `entrypoint` / `isSidechain` fields are what Claude Code writes today, not a documented format; a file without them counts as interactive. The hook's own `claude -p … -c` is not affected by any of this.

### Choice and fallback

New session: rows are read for every Claude account with no lead (`read_family(lead_key="")`), merged by email as usual; interactive `claude` sessions running on an account (from the process list and each process's `CLAUDE_CONFIG_DIR`, as for `claude -p` jobs) are added to its jobs; rows with no directory that is set up for sessions are dropped, and a merged row is represented by its first directory that is. Then the hook's `retry_blocked` and `score_rows` run unchanged. Before `choose`, accounts whose quota cannot be read are set aside as long as an account that can be read is not blocked: for a dispatch the hook gives an unreadable account the middle score, which can win a tie by config order; a session runs for hours and goes to a known account. The choice is made under the lease lock and reserved with an empty session id. Credits follow `QR_ALLOW_CREDITS=1` in the environment and `general.allow_credits`. The hook does not count interactive sessions; only `qr launch` does.

| Situation | Session starts on | Line |
|---|---|---|
| an account was chosen | that account | `session on <account>: <numbers and notes>`, plus the hook's `⚠` warnings (all low, old numbers) and `⚠ PAID OVERAGE` when it would bill |
| a conversation was found | its account, whatever its quota or set-up | `Resuming on …`, plus `⚠ <account>: <reason>` when its quota is used up and `⚠ PAID OVERAGE` when it would bill |
| conversation not found | default account | says so |
| every candidate blocked | default account | each account's reason; `⚠ PAID OVERAGE` when the default account would bill |
| no candidate has a reading | default account (nothing reserved) | says so |
| no account set up for sessions | default account | each account's reason (`launch = false`, no hook) |
| config missing or broken | default account | the path and the problem |
| an exception | default account | the exception class; recorded in `hook-errors.log` (`launch`) and `decisions.log` (`fail`) |
| not decided within `hook_budget_seconds` + 2 s | default account | says so; the choosing thread is abandoned |

"Default account" means `CLAUDE_CONFIG_DIR` removed from the environment, whatever account is logged in at `~/.claude`. Its set-up is not consulted for a fallback.

The line is printed only when stderr is a terminal, so `claude … 2>&1` in a script gets nothing extra; `decisions.log` has every decision either way.

### Starting

Through the sh launcher: `qr launch --resolve` prints the answer and the launcher `exec`s claude (above). `qr launch` run directly, and the Windows launcher: macOS and Linux `os.execve` with the chosen environment; Windows runs claude as a child with the chosen environment, leaves Ctrl+C to it and returns its exit status. Ctrl+C while the account is being chosen ends `qr launch` with status 130 without starting claude.

## Messages you will see

The hook's messages go to the lead session, attached to the Bash tool call; the launcher's go to the terminal (below). They are shown in English by default and in Traditional Chinese with `general.language = "zh-TW"` (or `QR_LANG=zh-TW`); the examples below are the English ones. `alice`, `bob`, `carol`, `dave` and the account keys are made up.

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

### From the launcher

One line on stderr before the session starts, only when stderr is a terminal. A new session routed:

```
[quota-router] session on bob: weekly 88% left (resets in 4.0 d)
```

Every other case, and what it means, is in the table under [Choice and fallback](#choice-and-fallback). The beginnings of those lines are `[quota-router] Resuming on the account that holds this conversation, …`, `[quota-router] Could not find which account holds this conversation; …`, `[quota-router] No account can take a new session (…)`, `[quota-router] No account's quota could be read; …`, `[quota-router] No Claude account is set up for new sessions (…)`, `[quota-router] No config at …`, `[quota-router] Config error in …`, `[quota-router] Choosing an account failed (…)` and `[quota-router] Choosing an account took longer than … s`; all of these end with `starting on the default account` except the first. Warnings that can be added: `⚠ <account>: <reason>; the session starts there anyway and may stop at once.`, `⚠ PAID OVERAGE: …` and `⚠ PAID OVERAGE POSSIBLE: …`.

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

- Only Claude Code's Bash tool is hooked. Subagents started with the built-in Agent/Task tool or a workflow run on the lead's own account; the [launcher](#sessions-and-the-claude-launcher) chooses that account when the session starts, and nothing changes it afterwards. A dispatch made inside a script is not in the command text, so it is neither routed nor checked (use `qr pick` in the script, see [Using `qr pick` in scripts](#using-qr-pick-in-scripts)).
- Text that is only data is left alone: quoted strings, comments and heredoc bodies, whatever the delimiter. A dispatch written inside a heredoc that is fed to a shell (`bash <<EOF … EOF`) is therefore not seen either.
- A Bash command is allowed or blocked as a whole. In `codex exec a; claude -p b`, a blocked Claude dispatch stops the Codex part too.
- Only the weekly window, the five-hour window and one per-model weekly bucket are modelled.
- `qr install` rewrites `settings.json` as standard JSON. Contents, key order and indentation width are kept; hand-aligned or single-line objects are re-wrapped. The backup is the untouched original.
- An account whose directory name contains a space is not credited with its running jobs on macOS and Linux (the process environment cannot be split reliably there); reservations still count.
- Launcher issues found in review and not fixed yet are listed under "Known issues" in [CHANGELOG.md](../CHANGELOG.md).

## Platform notes

| | macOS | Linux | Windows |
|---|---|---|---|
| Config file | `~/.config/quota-router/config.toml` | same | `%APPDATA%\quota-router\config.toml` |
| State directory | `~/.local/state/quota-router` (or `$XDG_STATE_HOME`) | same | `%LOCALAPPDATA%\quota-router` |
| Claude login read from | keychain, else `<dir>/.credentials.json` | `<dir>/.credentials.json` | `<dir>\.credentials.json` |
| Renewed Claude login written to | the keychain item it was read from (`security -i`), or that file | `<dir>/.credentials.json`, mode 600 | `<dir>\.credentials.json` |
| Codex login read from | `<dir>/auth.json` | same | same |
| Running jobs counted from | `ps`, including each job's environment | same | PowerShell `Get-CimInstance Win32_Process`, command lines only |
| Shell the command runs in | `sh` / `zsh` | `sh` / `bash` | Git Bash |
| `claude` launcher | `~/.local/share/quota-router/bin/claude` (or under `$XDG_DATA_HOME`) | same | `%LOCALAPPDATA%\quota-router\bin\claude.cmd`, untested |

The test suite runs on all three in CI. Day-to-day use so far has been on macOS; treat Windows as the least proven.

- **macOS.** The first keychain read may bring up a system permission prompt.
- **macOS and Linux.** The busy count reads each job's environment from `ps`. A directory with a space in its name cannot be told apart there from two separate words, so jobs on such an account are not counted; reservations still are.
- **Windows: Git for Windows is required.** Claude Code's Bash tool runs in Git Bash, and Bash commands are all this hook sees. Commands that Claude Code runs through its PowerShell tool are never routed.
- **Windows: the busy count is weaker.** Windows does not let one process read another's environment, so a running job is only attributed to an account when `CLAUDE_CONFIG_DIR=…` / `CODEX_HOME=…` is visible on its command line or its parent's. Jobs the hook routed to a named directory qualify. A job that got its account from an earlier `export` does not, and neither does a Claude job routed to the default account, because that one is written as `env -u CLAUDE_CONFIG_DIR` and names no directory. Reservations from the last five minutes are counted the same on every platform.
- **Windows: listing processes starts PowerShell**, which takes roughly 0.5–2 s of the hook's budget. Past 2 s nobody is counted as busy.
- **Codex: a renewal next to a running Codex.** Until 30 minutes before the expiry a Codex process on that `CODEX_HOME` (a job, a session, the desktop app's `codex app-server`) does not block a renewal, because Codex renews on its own only within five minutes of the expiry, after a 401 from its backend, or when the desktop app asks it to (`getAuthStatus` / `account/read` with a refresh flag), and in the last two cases re-reads `auth.json` first. If such a process hit a 401 for the old access token in the second between quota-router's request and its write, it could send the old refresh token once more; whether OpenAI then revokes the whole login is not known. Codex processes sharing one `auth.json` have the same exposure among themselves.
- **Codex: Codex can write back an older login.** Codex saves a renewal by reading `auth.json`, changing it and writing it in place, without a lock. When Codex read the file before quota-router replaced it and writes after, quota-router's new login is lost, possibly after quota-router's read-back confirmed it. That needs Codex to renew within the same moment, which it does only within five minutes of the expiry, after a 401 or when asked; quota-router renews days earlier.
- **Windows: an expired Claude login is seldom renewed.** The renewal treats a Claude Code process whose directory cannot be told as running on every account, and on Windows that is any session whose `CLAUDE_CONFIG_DIR` is not on a command line, including the one the hook runs in. A login kept in the Windows Credential Manager is neither read nor renewed; see [Renewing an expired Claude login](#renewing-an-expired-claude-login).
- **Windows: paths** are written into commands with forward slashes (`C:/Users/me/.claude-b`). In your own commands and in the config file, `C:\…`, `C:/…`, `~/…` and the Git Bash spelling `/c/…` are all understood; the config loader converts `/c/…` to a drive path on Windows.
- **Windows: logins kept elsewhere are not read.** That includes a Console sign-in under `%APPDATA%\Anthropic` and a Codex login kept in the system credential store.
- **Windows: the launcher is untested.** `qr install` writes a batch file, `claude.cmd`, which runs `qr launch`; there `claude` runs as a child process instead of replacing it. The logic is covered by the test suite, but the file has not been run on a real Windows machine.

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
| `claude` launcher | `%LOCALAPPDATA%\quota-router\bin\claude.cmd`, a batch file running `qr launch`; claude runs as a child process and its exit status is passed on | **Untested on a real machine.** A `%` in the interpreter or package path would break the batch file. Typed in PowerShell or `cmd`, `claude` finds it through `PATH` / `PATHEXT` like any `.cmd`. |
| Finding a resumed conversation | `<dir>\projects\<slug>\*.jsonl`, where the slug replaces every character that is not a letter or digit with `-` | The slug rule was observed on macOS and carried over; it has not been compared with what Claude Code produces on Windows |
| Shell | Only commands of the Bash tool (Git Bash) | Commands run through Claude Code's PowerShell tool are never routed |
| Console output | The hook's stdout/stderr are UTF-8; other `qr` commands keep the console encoding and replace what it cannot show with `?` | On a legacy code page, emoji and Chinese text in `qr status` may show as `?` |

See [research-windows.md](research-windows.md) for the sources these choices are based on.

## Privacy and security

**What it reads**:

- The Claude Code OAuth token: on macOS from the login keychain through `security find-generic-password`, otherwise from `<dir>/.credentials.json`. One keychain service can hold more than one item (one with only MCP logins, one with the Claude login), so the item filed under your user name is asked for first, then any item of that service, then the file. Details: [Where a Claude login is read](#where-a-claude-login-is-read).
- The Codex token in `<dir>/auth.json`.
- As fallbacks when a query fails: the usage Claude Code cached in `.claude.json`, and the last `rate_limits` record in Codex's `<dir>/sessions/**.jsonl`. To find that record up to 20 of the most recent session files are read line by line; only the record is kept.
- The names and modification times of conversation files in the account directories, to find which account holds a resumed conversation. For `claude --continue` through the launcher, also the first lines of those files in the current directory's project folder, only to tell a `claude -p` run or a subagent from an interactive session.
- The process list, to count running `claude` / `codex` jobs per account. That means the command lines of running processes, which can contain prompt text, and on macOS and Linux the environment of the `claude` / `codex` ones. Only the program name and `CLAUDE_CONFIG_DIR` / `CODEX_HOME` are used; none of it is stored.
- `qr doctor` only: your shell startup files (`~/.zshrc`, `~/.bashrc` and a few more), to find a `claude` alias or function. Only those lines are shown, with anything that looks like a credential masked; nothing is stored.

Logins are only read, with one exception per family that is off unless `refresh_logins = true` under that family: when a Claude login's access token has expired and no Claude Code process runs on that directory, or a Codex login's expires within `refresh_before_hours` and no Codex process on that `CODEX_HOME` may be renewing it itself, a background quota-router process renews it with the refresh token, as the CLI would, and writes the renewed login back where it came from (the keychain item or `.credentials.json`; `auth.json`). It never copies a token anywhere else. Details and how to turn it on: [Renewing an expired Claude login](#renewing-an-expired-claude-login), [Renewing a Codex login](#renewing-a-codex-login).

**Where it sends anything.** Each token goes to its own vendor's endpoints and nowhere else. HTTP redirects are never followed; a redirect counts as a failed query:

- `https://api.anthropic.com/api/oauth/usage`
- `https://api.anthropic.com/api/oauth/profile`
- `https://platform.claude.com/v1/oauth/token`: only the refresh token of an expired Claude login, only when it is being renewed
- `https://chatgpt.com/backend-api/wham/usage`
- `https://auth.openai.com/oauth/token`: only the refresh token of a Codex login, only when it is being renewed

quota-router itself sends no telemetry and makes no other request, unless you change `[endpoints]`.

**The background Codex probe.** When a live Codex query fails and the local numbers are over two hours old, one `codex exec` no-op ("Reply with the single word: ok", read-only sandbox, low reasoning effort) is started in the background to refresh them. That is the real Codex CLI running on that account: it talks to its vendor, may refresh its own login and writes a session file, all outside the guarantees above. It uses the model that account's Codex config selects, so what it costs depends on that config; it is small but not zero. It is not started when the account is known to have credits and its subscription quota is used up, nearly used up or unknown; when the credit balance itself is unknown, it is. It happens at most once per account per `codex_probe_hours` (two hours), and `QR_NO_PROBE=1` turns it off.

**What it writes**, all in the state directory:

- State files: a usage cache, reservations, usage and reset history, a record of what `qr install` installed, and for `--continue` the sessions started through the launcher (a hash of the working directory, the account key and the time; never the path). These hold account emails, short hashes of tokens (never the tokens) and the session ids of recent reservations.
- `decisions.log`: one line per decision with the time, the lead's account key, the first 8 characters of the session id (or a hash of it), the chosen account key, family, kind of decision, quota numbers, model class and a 12-character hash of the command. No command text, no prompt, no email, no directory.
- `hook-errors.log`: one line per error with the time, operation name, exception class (with the HTTP status code for an HTTP error answer), code location and the account key or state file concerned. No exception message and no traceback, because those can quote headers and response bodies.
- `probe-<account>.log`: whatever the `codex` CLI printed during a background probe.
- `refresh-<account>.log`: one line per background login renewal, its time and outcome (`renewed`, `failed`, …); `refresh-<account>.start` marks when the last one started.
- `notify.log`: for each run of the `[notify]` command, a line with the time, family, account key and reason, whatever the command printed, and its exit status. What the command prints is up to it. `notify-state.json`: when it last ran per account and reason, and whether it succeeded.
- `*.lock` files and, briefly, `*.tmp` files.
- `debug.log`, **only with `QR_DEBUG=1`**: full tracebacks. A pattern-based scrub is applied, but it is best effort; this file can contain tokens, headers, response bodies and config values. Treat it as sensitive and delete it when you are done.

Outside the state directory it writes only what `qr install` adds (the hook entry in each `settings.json` you install into, and the `claude` launcher file) and, when it renews a login, that login in the CLI's own store (Claude Code's keychain item or `.credentials.json`, plus its lock directories for the moment it takes; Codex's `auth.json`). The `[notify]` command, when you set one, can of course do anything.

The field-by-field formats are under [Log files](#log-files).

**The hook rewrites commands.** That is its job, and it is also the risk. A program that runs before every Bash command and can change it is a privileged thing to install. What it does is narrow: it inserts an environment prefix in front of a `claude` or `codex` invocation, or blocks the whole command; it changes nothing else in the command. A bug could still send a job to the wrong account or break a command it misreads (see [Known limitations](#known-limitations)). Read `qr decisions` after your first few dispatches. Anyone who can edit the config file can add accounts or redirect `[endpoints]`, and with them your tokens, so protect it like `settings.json`.

### Where a Claude login is read

macOS: the login keychain, through `security find-generic-password … -w` (writing happens only when a login is renewed; see the next section). One service name (`Claude Code-credentials`, or `Claude Code-credentials-<8 hex digits of the SHA-256 of the directory>` for other directories) can hold more than one item: one filed under the account name `unknown` with only MCP logins, one filed under the macOS user name with `claudeAiOauth`. `security -s <service>` alone returns whichever it finds first, so the item filed under the user name (`-a <user>`) is asked for first; then any item of the service; then `<dir>/.credentials.json`. The first one with `claudeAiOauth` is used; when none has it, whatever the keychain returned. A query that does not answer within its time is not repeated. Linux and Windows: `<dir>/.credentials.json` only.

### Renewing an expired Claude login

Claude Code renews its login only when it is used, so an account nobody uses for about a day ends up with an expired access token. Without renewal quota-router cannot read that account's quota, ranks it in the middle and so does not prefer it, and it stays unused and unread. With `refresh_logins = true` (off by default, because it writes to Claude Code's credential store) quota-router renews it itself, in a background process:

- **When.** A reading (the hook, `qr status`, `qr pick`, `qr check`, `qr launch`) finds that the access token expires within five minutes (Claude Code uses the same margin), the login has a refresh token whose `refreshTokenExpiresAt`, when stored, has not passed, and its stored scopes include `user:inference` (a Claude.ai login, the kind Claude Code itself renews).
- **Not when** a Claude Code process of any kind (a session, a `claude -p` job, a subcommand, a resident helper) runs on that directory, judged from the process list: that process renews the login itself. The check fails closed: when the process list cannot be read, or a Claude Code process's directory cannot be told, the account counts as busy. On Windows another process's environment is unreadable, so any Claude Code process whose `CLAUDE_CONFIG_DIR` is not written on its own or a parent's command line (a session started through the launcher, the session the hook runs in) keeps every account from being renewed. A busy account, or a login that cannot be renewed, is looked at again after a minute; a failed attempt after ten minutes. In all these cases the account reads as before: "login token expired" and the last known numbers.
- **In the background.** The reading never contacts the token endpoint. It starts one detached process (`python -I <package>/__main__.py renew-login <account>`, output appended to `refresh-<account>.log` in the state directory), records `refresh-started` in `decisions.log` and reports the login as it found it; the next reading uses the renewed login. At most one renewal is started per account every two minutes, and only one runs at a time. `qr status --refresh` on a terminal waits for the renewals it started, up to 35 s, then shows the new numbers; the hook, `qr pick`, `qr check` and `qr launch` never wait. The reason: the token endpoint rotates the refresh token, so once it has answered, the old refresh token is dead and the new one must be saved or that directory is signed out. A hook has eight seconds; the background process waits up to 30 s for the answer, as Claude Code does, and nothing in it is cut short by a time budget.
- **Locks.** The background process takes a lock of its own and Claude Code's three lock directories for that directory: `<dir>/.oauth_refresh.lock`, the older `<dir>.lock`, and `<dir>/.storage-write.lock`, which Claude Code holds for every change to its stored login (an MCP login, a logout, its own renewal). When Claude Code holds one of them the renewal is skipped; one Claude Code has abandoned (not renewed for 60 s, 15 s for `.storage-write.lock`) is taken over, as Claude Code itself would. While it holds them it renews their modification time every five seconds, as Claude Code does. It checks again for Claude Code processes, reads the login again inside the locks, and does not ask at all when someone else has renewed it meanwhile.
- **How.** One POST, JSON, to `endpoints.claude_token`: `grant_type=refresh_token`, the refresh token, Claude Code's public client id (or the one stored with the login) and the scopes stored with the login, with the header `User-Agent: quota-router/<version>`. This is the request Claude Code 2.1.296 sends, except that Claude Code first asks for its full current scope list and falls back to the stored scopes; quota-router asks for the stored scopes straight away. No redirect is followed. The real endpoint sits behind Cloudflare, which refuses Python's default `User-Agent` with HTTP 403 before the request arrives. Observed on 2026-10-13: the answer carries a new access token valid for 8 hours (`expires_in` 28800), a **new refresh token** (the one sent stops working) valid for about 21.6 days (`refresh_token_expires_in`), and the scopes; the `token_uuid`, `organization` and `account` it also carries are not stored. After a handful of failed requests within a minute it answers HTTP 429.
- **Where it is written.** Back to where it was read: on macOS the same keychain item (`security add-generic-password -U` on the same service and account, the JSON hex-encoded and given to `security -i` on stdin, as Claude Code writes it), otherwise `<dir>/.credentials.json` through a temporary file with mode 600 that is renamed over it. A `.credentials.json` that does not exist is never created. Only `accessToken`, `refreshToken`, `expiresAt`, `refreshTokenExpiresAt` and `scopes` change; every other field and every other top-level entry (`mcpOAuth`) is kept as it is at that moment. It is written only when the stored refresh token is still the one that was sent, with up to three attempts, and read back afterwards. From the request until the write is confirmed, the process ignores Ctrl-C, `SIGTERM` and `SIGHUP`; on Windows only Ctrl-C and Ctrl-Break can be ignored, and ending it from Task Manager or with `taskkill /F` cannot be prevented.
- **The command line.** On stdin the login is on no command line. Like Claude Code, a `security` command longer than 4032 characters (a login stored next to several MCP logins) is passed as arguments instead; then the hex-encoded login, refresh token included, is visible to other local users through `ps` for the moment `security` runs. Claude Code itself does the same for that item.
- **Odd answers.** An answer that carries a new refresh token but no usable access token (or one without `user:inference`) still has its refresh token saved, since the old one is dead by then; it counts as a failed attempt. An unusable `expires_in` is taken as one hour.
- **When it fails.** A failed or refused request leaves the stored login untouched and the account reads as before. A 429 or any other error answer is a failed attempt (tried again after ten minutes, never twice in one run). A refresh token the server refuses (`invalid_grant`) is shown as "login can no longer be renewed: run `claude login` with this account's directory" and is not sent again; an account on hold gets its own message and is tried again after ten minutes. When the renewal worked but the write did not, the new refresh token may be lost, which can sign that directory out: that is recorded in `hook-errors.log` (`login-refresh-save`) and shown on the account until the next attempt. A write that succeeded but could not be read back is recorded as `refresh-unverified`, not as a failure.
- **What is recorded.** One `decisions.log` line when a renewal starts and one when it ends, source `refresh` (see below); a failure also in `hook-errors.log`, with the HTTP status code (`HTTPError-429`). No token, answer body or `error_description` ever reaches a log or a state file; `cache.json` keeps only a 12-hex-digit hash of a refused refresh token, to know not to send it again.
- **Turning it on and off.** It is off by default. `refresh_logins = true` under `[families.claude]` turns it on; `QR_NO_REFRESH=1` in the environment turns it off whatever the config says.
- **Windows Credential Manager.** Claude Code can keep its login in the Windows Credential Manager instead of `.credentials.json` (a server-side switch, or `CLAUDE_CODE_FORCE_WINDOWS_CREDMAN`). quota-router cannot read it there. When Claude Code moved the login there but could not delete the old `.credentials.json`, quota-router would renew that leftover copy, and the renewal would invalidate the refresh token both copies share. It does not renew on Windows when `CLAUDE_CODE_FORCE_WINDOWS_CREDMAN` is set in its environment; the server-side switch it cannot see. Delete a leftover `.credentials.json`, or set `refresh_logins = false`, if you use the Credential Manager.

### Renewing a Codex login

Codex renews its login only when it is used: when the access token (a JWT that lasts about ten days) expires within five minutes, or after the backend refuses it. An account nobody used for that long meets an expired access token on its next `codex exec`; if the refresh token has stopped working meanwhile, that run fails with "Your access token could not be refreshed. Please log out and sign in again", and so does every scheduled job after it, with nobody told. With `refresh_logins = true` under `[families.codex]` (off by default, because it writes to Codex's `auth.json`) quota-router renews the login ahead of time, and with `[notify]` it tells you when it cannot (see [When a renewal fails](#when-a-renewal-fails)).

- **When.** A reading (the hook, `qr status`, `qr pick`, `qr check`) finds that the access token expires within `refresh_before_hours` (default 72; Codex's own margin of five minutes is far too short for readings that may come once a day and not at all on some days). The expiry is the access token's `exp`; when that cannot be read, `last_refresh` plus eight days, which is what Codex itself uses then. The login has to be Codex's own ChatGPT login: `auth_mode` `chatgpt` (or none) and a refresh token. An API key, or tokens a host hands Codex, are left alone.
- **Not when** a Codex process on that `CODEX_HOME` may renew the login itself, judged from the process list. `codex login` and `codex logout` always count: they write `auth.json` at any time. The subcommand is found after Codex's top-level options (`-c`, `--profile`, `-C` and the others that take a value are skipped with it). Any other Codex process (`codex exec`, an interactive `codex`, `codex resume`, the ChatGPT / Codex desktop app's `codex app-server`, also as its daemon, `exec-server`) counts from 30 minutes before the expiry on, and after it, however long ago: Codex renews by itself when the token expires within five minutes, which once it has expired is every time it is used. Before that it renews only after its backend refuses the token with a 401, or when the desktop app asks it to (`getAuthStatus` with `refreshToken`, or `account/read` with `refresh_token`), and all of these re-read `auth.json` first and take over what another process saved there; so a process running days before the expiry does not block a renewal (see [Known limitations](#known-limitations)). It fails closed as for Claude: an unreadable process list, or a counting Codex process whose `CODEX_HOME` cannot be told, counts as busy. A busy account is looked at again after a minute, a failed attempt after ten minutes.
- **In the background**, as for Claude: the reading starts one `renew-login <account>` process and never contacts the token endpoint itself. That process takes the account's lock, reads `auth.json` again, checks the processes again, and does not ask when someone else has renewed the login meanwhile. Codex takes no lock on `auth.json` that another program could see, so none is taken there.
- **How.** One POST, JSON, to `endpoints.codex_token` (`https://auth.openai.com/oauth/token`): `client_id` `app_EMoamEEZ73f0CkXaXp7hrann` (Codex's public client id), `grant_type=refresh_token` and the refresh token, with `User-Agent: quota-router/<version>`. This is the request Codex 0.160.1 sends (`codex-rs/login/src/auth/manager.rs`). The answer may carry `id_token`, `access_token` and `refresh_token`. quota-router assumes the refresh token is rotated, so once the server has answered, the new one must be saved.
- **Where it is written.** `auth.json` in that `CODEX_HOME` is replaced as a whole: a temporary file next to it with mode 600, written and flushed to disk, renamed over it (a symbolic link is followed, so the file it points to is replaced). `tokens.access_token`, `tokens.refresh_token` (when the answer has a new one), `tokens.id_token` (only when it is a readable JWT, since Codex refuses to load an `auth.json` with one that is not) and `last_refresh` change; `account_id`, `auth_mode`, `OPENAI_API_KEY` and everything else stay as they are at that moment. It is written only while the stored refresh token is still the one that was sent, with up to three attempts, each read back. An `auth.json` that does not exist is never created. From the request until the write is confirmed, Ctrl-C, `SIGTERM` and `SIGHUP` are ignored.
- **When it fails.** Any 401, a 400 `invalid_grant`, or the codes `refresh_token_expired`, `refresh_token_reused` and `refresh_token_invalidated` whatever the status code (Codex's own list of permanent failures) mean the refresh token is dead, unless `auth.json`, read again after 0.5, 2 and 5 seconds, holds another refresh token by then (Codex or a new login replaced it meanwhile; recorded as `refresh-adopted`, nothing is told). When the last of those reads finds no readable login (Codex writes the file in place and may be caught half done) it is a failed attempt, not a dead login. A dead one: the account shows "login can no longer be renewed: run `codex login` with CODEX_HOME set to this account's directory", the token is not sent again, and the `[notify]` command runs. Other error answers (429, 5xx, a 403 from a proxy) are a failed attempt, tried again after ten minutes. A renewal that could not be saved is `refresh-unsaved` and is shown on the account.
- **Recorded** as for Claude: `decisions.log` lines with source `refresh` and family `codex`, `hook-errors.log` for failures, `refresh-<account>.log`. No token, answer body or error description is logged.
- **Turning it on and off.** `refresh_logins = true` under `[families.codex]`; `QR_NO_REFRESH=1` turns it off whatever the config says.

### When a renewal fails

`[notify] on_refresh_failed` names a shell command (run with `/bin/sh -c`; on Windows `cmd /c`). It runs when a background renewal of a Claude or Codex login ends as:

| `QR_REASON` | Meaning | What to do |
|---|---|---|
| `dead` | The refresh token was refused or has expired. The login is gone. | Log in again in that directory (`claude login` / `codex login` with its `CLAUDE_CONFIG_DIR` / `CODEX_HOME`). |
| `unsaved` | Renewed, but the new login could not be saved; the directory may be signed out. | Check the account; log in again if it no longer works. |
| `failed` | The request failed (no answer, 429, 5xx, an odd answer). It is tried again after ten minutes. | Usually nothing; repeated alerts mean something is wrong. |
| `on-hold` | Claude only: the account is on hold. | See claude.ai. |

Its environment is `QR_FAMILY` (`claude` / `codex`), `QR_KEY` (the account key), `QR_REASON`, `QR_DETAIL` (the `decisions.log` phrase), `QR_DIR` (the account's directory) and, when known, `QR_EMAIL` (Codex: from the `id_token`; Claude: from the cached profile) and `QR_EXPIRES_AT` (the access token's expiry, ISO 8601 local time). Of quota-router's own environment it gets only `PATH`, `HOME`, `LANG`, `LC_*` and `TMPDIR` (on Windows also `SYSTEMROOT`, `COMSPEC`, `PATHEXT`, `TEMP`, `TMP`, `USERPROFILE`); a command that needs anything else sets it itself. Busy, nothing-to-renew, renewed and similar outcomes never run it. agy has no login renewal, so it never runs for agy.

- **Once per account and reason.** It runs at most once per account and reason every `repeat_hours` (default 12); when the command itself failed (non-zero exit, timeout, could not start, or killed before it could report back), it may run again after an hour. A temporary failure (`failed`) runs it only after `failed_attempts` (default 2) in a row, so one 429 between two renewals is not a message; failures more than `repeat_hours` apart do not count as in a row. A renewal that succeeds clears both, so the next failure of that account is counted from the start. The record is `notify-state.json` in the state directory; when its lock cannot be had within five seconds, that alert is skipped (one too few rather than one too many) and `decisions.log` says so.
- **It cannot hold anything up.** It runs in the background renewal after the outcome has been recorded, the account's locks released and the signal shield left, with stdin closed and a 30-second limit; on macOS and Linux it runs in a session of its own, and at the limit that whole process group is ended (the shell and, say, the curl it started), so nothing it started keeps running and sends later. Its output goes to `notify.log`. Whatever it does or fails to do changes nothing about the renewal. Each run is one `decisions.log` line: `notify` (`alert command ran`), `notify-failed` (`alert command failed`) or `notify-skipped` (`alert already sent`, `alert waits for another failure`, `alert skipped, state lock busy`).
- **It only covers renewals quota-router attempts.** A login that is never due (renewal off, or always busy) and a refresh that Codex or Claude Code itself attempts and fails are not seen.

### Log files

Both are in the state directory (`$QR_STATE`, else `~/.local/state/quota-router` or `$XDG_STATE_HOME/quota-router`; Windows `%LOCALAPPDATA%\quota-router`). They are always in English, whatever `general.language` says.

#### `decisions.log`

One line per decision, nine fields separated by ` | `:

```
10-11 21:14:10 | hook | lead=claude-main | sess=3f9a1c2e | claude | auto | pick=claude-b | weekly 80% left; model other | cmd#5d41402abc4b
10-11 21:14:12 | hook | lead=claude-main | sess=3f9a1c2e | codex | deny | pick=- | blocked: weekly quota; model other | cmd#9b74c9897bac
10-11 21:20:03 | qr launch | lead=- | sess=- | claude | launch | pick=claude-b | weekly 80% left; model fable | cmd#0c3e5f1a9d27
```

| Field | Content |
|---|---|
| time | `MM-DD HH:MM:SS`, local time |
| source | `hook`, `qr pick`, `qr check`, `probe`, `qr launch` or `refresh` |
| `lead=` | key of the account the dispatching session runs on, with ` (assumed)` when it was not detected; `?` when unknown; `-` for `qr launch`, which starts a session and has no lead |
| `sess=` | first 8 characters of Claude Code's session id; a hash when the id has an unusual shape; `-` when there is none |
| family | `claude`, `codex`, `agy`, `kimi`, `grok` or `?` |
| kind | `auto` (routed), `explicit` (account named in the command), `explicit-unknown` (named directory not in the config), `resume`, `resume-unknown`, `deny`, `fail`, `pick`, `check`, `probe`; for `qr launch`: `launch` (a new session routed), `launch-resume` (a resumed conversation kept on its account), `launch-default` (started on the default account; `pick=` is the key of the account at the default location, or `-`), `fail`; for `refresh` (a Claude or Codex login, `pick=` the account): `refresh-started` (a background renewal was started), `refresh` (renewed and saved), `refresh-adopted` (someone else had renewed it meanwhile, or a Codex login is no longer due), `refresh-busy` (Claude Code or Codex uses the directory, or Claude Code holds its lock), `refresh-none` (nothing to renew with), `refresh-failed`, `refresh-dead` (the refresh token was refused or has expired), `refresh-on-hold`, `refresh-unsaved` (renewed, but the write failed), `refresh-unverified` (written, but could not be read back), and for the `[notify]` command `notify`, `notify-failed`, `notify-skipped`. A pass-through (`--help`, a subcommand, `CLAUDE_CONFIG_DIR` already set, inside a session) is not logged. |
| `pick=` | account key; `-` when there is none |
| detail | `; `-separated: a fixed phrase, the numbers behind the choice (`weekly N% left`, `five-hour N% left`, `N running, N reserved`), `model <class>`, `credits allowed`. The phrases `qr launch` adds: `launched with a warning`, `on paid overage`, `no account can take it`, `no account could be read`, `no account is set up for sessions`, and `conversation location unknown` / `kept the original account` as for the hook. For `refresh`: `renewal started in the background`, `login renewed`, `already renewed elsewhere`, `Claude Code may be using that directory`, `Claude Code holds its lock`, `another renewal is running`, `nothing to renew with`, `renewal failed` (with `, HTTP <code>` after an error answer), `odd answer; new refresh token kept`, `refresh token refused; run claude login there`, `Codex may be using that directory`, `refresh token refused; run codex login there`, `account on hold`, `renewed but could not be saved`, `renewed and saved; read-back failed`, `alert command ran`, `alert command failed`, `alert already sent`, `alert waits for another failure`, `alert skipped, state lock busy`. For `deny` only the category: `blocked: credits`, `paid overage`, `lead protection`, `five-hour window`, `weekly quota`, `wrong account for resume`, `no account available`, `other`. For `fail` the exception class name. |
| `cmd#` | first 12 hex digits of the SHA-256 of the command; `-` when there is no command. For `qr launch` the command is `claude` followed by its arguments, shell-quoted. |

Never written: the command or prompt, an email address, a directory, a note in the display language, an exception message. An account key, lead key, model class or session id that contains anything but letters, digits, `_`, `.` and `-` is written as `#` plus 12 hex digits of its hash. When several dispatches share one Bash command and one of them is denied, only the denial is logged.

#### `hook-errors.log`

One line per event, five fields:

```
2026-10-11 21:02:02 | usage-query | ValueError | core.py:430:fetch | claude-b
2026-10-11 21:02:03 | lock-timeout | - | - | leases
2026-10-11 21:02:09 | skipped-history | - | - | -
```

Time, operation, exception class (an HTTP error answer adds its status code: `HTTPError-403`), code location (`file:line:function` of the innermost frame inside this package), subject (a state file, a lock name or an account key). Operations: `hook` (the hook itself failed; this is what "routing failed N times" counts), `launch` (`qr launch` failed and started the default account), `launch-timeout` (`qr launch` gave up waiting for its choice), `usage-query`, `login-refresh` (renewing a login failed), `login-refresh-start` (the background renewal could not be started), `login-refresh-save` (a renewed login could not be saved; see [Renewing an expired Claude login](#renewing-an-expired-claude-login)), `notify` (the `[notify]` step itself failed, not the command), `state-save`, `lock-timeout`, `lock-failed`, `unlock-failed`, `skipped-decision-log`, `skipped-history`, `skipped-notices`, `skipped-failure-count`.

No exception message and no traceback is written here, because both can quote request headers, response bodies and config values. With `QR_DEBUG=1`, full tracebacks are appended to `debug.log`. A pattern-based scrub is applied to that file (`Bearer …` / `Basic …` values, strings starting with `sk-`, JWT-shaped strings, `NAME=<20 or more characters>` unless the value looks like a path), but it is best effort.

#### Other files in the state directory

| File | Content |
|---|---|
| `cache.json` | Last reading per account: usage windows, when fetched, account email, a short hash of the token (never the token), error text, back-off state |
| `leases.json` | Reservations of the last `lease_seconds`: account key, time, full session id |
| `installs.json` | What `qr install` put into each `settings.json`, and the launcher file it wrote (see [`qr install` and ownership of the hook entry](#qr-install-and-ownership-of-the-hook-entry) and [The launcher file](#the-launcher-file)) |
| `launched.json` | Sessions started through the launcher or with `CLAUDE_CONFIG_DIR` written out, for `--continue`: a hash of the working directory (never the path), the account key, the time; 30 days, at most 500 |
| `failure-index.json` | Offsets into the two logs for counting recent failures |
| history, observation and notice files | Usage readings over time, detected reset events, which session has been told about which reset |
| `probe-<key>.log`, `probe-<key>.lock` | Output of the background Codex probe, and its once-per-interval marker |
| `refresh-<key>.log`, `refresh-<key>.start` | Outcome of each background login renewal (and agy query), and when the last one started |
| `notify.log`, `notify-state.json` | Each run of the `[notify]` command with its output and exit status; when it last ran per account and reason |
| `*.lock` | Lock files; never hold data |

## Uninstall

```sh
qr uninstall                              # and once per --config-dir you installed into
pip uninstall quota-router
```

`qr uninstall` removes the entry `qr install` wrote, recognised by its exact command and argument list, and nothing else; entries written by hand, by another copy, or that merely mention this package are left alone. Without `--config-dir` it also removes the `claude` launcher when the file is exactly what `qr install` wrote (`--no-launcher` keeps it; `--config-dir` never touches it). The rules are in [`qr install` and ownership of the hook entry](#qr-install-and-ownership-of-the-hook-entry) and [The launcher file](#the-launcher-file). The config file, the state directory, the launcher's empty directory and the `PATH` line in your shell startup file stay behind for you to delete.

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
| Windows | Seven tests (fifteen cases) in `tests/test_launch.py` | Run the launcher through `/bin/sh`, use XDG directories, or check the POSIX `export PATH=` line, file modes and shell aliases |
| macOS, Linux | One test in `tests/test_final_review.py` | Needs Windows path rules (a drive path is absolute only there) |
| Anywhere without a cached `setuptools` and `uv` | `tests/test_fresh_machine.py` | It installs into a new virtual environment without the network. Set `QR_E2E_ONLINE=1` to let it download instead; CI does. |

A test in `tests/test_specs.py` scans the spec sources and fails if the Windows skip list and the files that actually use `fcntl`, `getitimer` or `/bin/zsh` ever differ. There is no expected-failure list on any platform. On Linux the specs run with the platform pinned to the macOS branch, because the operating-system layer is faked in them with macOS-shaped data; each platform's own code is tested in `tests/test_platform_compat.py`.

#### What the fresh-machine test does and does not show

It creates a virtual environment, installs the package from a copy of the source, runs `qr init`, `qr install`, `qr doctor`, feeds the installed hook command a real `PreToolUse` event against a local stand-in for the usage endpoints, checks the rewritten command and the logs, runs `claude` through the installed launcher (a stand-in prints the account it was started on), and uninstalls. It launches the hook command itself, exactly as written into `settings.json`. It does not run Claude Code, so it does not show that a given Claude Code version accepts the exec-form entry (`command` plus `args`).


## Kimi Code family

The fourth family is `kimi`; it selects subscriptions within Kimi only. See the README for the account and model configuration. `dir = ""` resolves to `~/.kimi-code`; other directories are selected with `KIMI_CODE_HOME`. The default location adds no directory prefix. An inherited `KIMI_CODE_HOME` is treated as an explicit account selection so an unprefixed command cannot accidentally use a different account. CLI executable basenames `kimi`, `kimi.exe`, and `kimi.cmd` are supported, including paths. Only `-p` / `--prompt` runs are dispatches. Model options are preserved, and no permission flags (`--auto`, `-y`) are added. Session/configuration files are not copied or edited.

Usage comes from `GET /coding/v1/usages`. `usages.limit_5h.used_ratio * 100` is five-hour usage, `limit_month_total` is monthly usage, and `limit_month_code` is separately shown as the code monthly remainder. Internally the monthly total uses the common `seven_day` key so selection, reservations, reset history and scoring use the same machinery. Every Kimi long-window display is labelled monthly, including status bars/table, pick reasons, summaries, and report events. `weekly_floor`, `tight_warning`, and long-window hint thresholds apply to this **monthly** window. `usage_cache_seconds` controls freshness; a passed reset also requests a fresh reading. The existing three families' scores are unchanged. `qr report` shows the monthly remainder and reset; it does not apply the weekly pace formula because the API supplies no monthly cycle start.

| Endpoint setting | Default |
|---|---|
| `endpoints.kimi_usage` | `https://api.kimi.com/coding/v1/usages` |
| `endpoints.kimi_profile` | `https://api.kimi.com/coding/v1/me` |
| `endpoints.kimi_token` | `https://auth.kimi.com/api/oauth/token` |
| `endpoints.kimi_token_fallback` | `https://auth.kimi.ai/api/oauth/token` |

Usage/profile tokens can only go to exact `api.kimi.com` or `api.kimi.ai` HTTPS hosts; refresh tokens only to exact `auth.kimi.com` or `auth.kimi.ai`. Port 443 only, no URL credentials, query or fragment, and no redirects. Tests replace the transport with fake responses and never weaken this validation. `KIMI_CODE_OAUTH_HOST`, then `KIMI_OAUTH_HOST`, overrides the primary OAuth host subject to the same validation. `.com` is the default irrespective of the region file; a failed primary can try the configured fallback once. A successful POST whose answer cannot be saved is never repeated against the fallback.

`GET /coding/v1/me` provides email and `user_level_name`. Usage and account identity are checked against the current credential fingerprint before caching; a changed login invalidates old data. Tokens and response/error bodies are never logged. Only token fingerprints are cached.

`[families.kimi] refresh_logins = true` is the default because access tokens last about 15 minutes. A reading that needs a token within 30 seconds of expiry sends a form POST with `grant_type=refresh_token`, the stored refresh token, and CLI client ID `17e5f671-d194-4dfb-9706-5516cb48c098`. New token fields and expiry are merged into `<dir>/credentials/kimi-code.json`, retaining other CLI fields. A temporary mode-600 file is flushed, fsynced and renamed; stored credentials are compared before replacement and checked afterwards. The empty `<dir>/oauth/kimi-code` sentinel is not read as a credential.

The installed Kimi Code 2.3.0 bundle uses a proper-lockfile directory `<dir>/oauth/kimi-code.lock` around refresh, with a five-second stale limit. On macOS/Linux the router takes the same lock, updates its timestamp every second, and releases it only if it still owns it. It also holds its own lock per credential path. On Windows Kimi disables its cross-process OAuth lock, so the router only has its own lock and credential comparisons; the CLI can still race it in the final comparison/replacement interval. Forced process termination and programs that ignore the lock cannot be made fully safe.

The hook and time-budgeted reports start `refresh-kimi <key> <owner>` detached when renewal is required; their current reading is unknown and can still be dispatched. Normal manual queries perform renewal directly. A worker's result is accepted only while it owns the cache claim; repeated callers do not start duplicates. Failure enters a cooldown, clears quota to unknown, and leaves an existing login eligible for selection. A directory with neither access nor refresh token is not eligible. `QR_NO_REFRESH=1` or `refresh_logins = false` prevents refresh. `~/.kimi-code/config.toml` is never touched.

## Grok Build family

The fifth family is `grok`. `dir = ""` resolves to `~/.grok`; other directories are selected with `GROK_HOME` (Grok's documented home override). The default location adds no prefix, and an inherited `GROK_HOME` counts as an explicit account selection, as for Kimi. `grok`, `grok.exe` and `grok.cmd` are recognised, including paths. A dispatch is a run with `-p` / `--single`, `--prompt-file` or `--prompt-json` (clap reads options anywhere, so every word is looked at; option values are skipped). `grok` without these, `grok "initial prompt"`, `--help` / `-v`, and a first plain word that is a subcommand (`models`, `login`, `agent`, `sessions`, …) are not dispatches. `-m` / `--model` / `-m<model>` is kept; otherwise `families.grok.default_model` is added after the program name when it is not empty.

Resumed conversations: Grok keeps each one in `$GROK_HOME/sessions/<working directory, percent-encoded>/<session id>/`. `-r <id>` / `--resume=<id>` is looked up by id in every Grok account; `-c`, `--continue` and a bare `-r` take the account with the newest conversation for the working directory. A title (`-r "my title"`) is not looked up: the dispatch runs as written, with the usual "conversation location unknown" note.

Usage comes from `GET https://cli-chat-proxy.grok.com/v1/billing?format=credits` with the headers the CLI sends (`Authorization: Bearer`, `x-grok-client-identifier: grok-shell`, `x-grok-client-version`). This endpoint is not documented by xAI; it is the one Grok Build 1.0.46 uses. `config.creditUsagePercent` (else the `GrokBuild` entry of `productUsage`) is the used %, `currentPeriod.end` the reset and `currentPeriod.start` the start; the type (`USAGE_PERIOD_TYPE_WEEKLY` today) is shown in `qr status` when it is not weekly. Internally the period uses the common `seven_day` key, and `qr report` paces it over its real length. There is no five-hour window. Readings follow `usage_cache_seconds` and the hook's time budget; when the budget is too short the cached reading is used.

| Setting | Default |
|---|---|
| `families.grok.default_model` | `""` (add nothing) |
| `families.grok.refresh_logins` | `false` |
| `endpoints.grok_billing` | `https://cli-chat-proxy.grok.com/v1/billing?format=credits` |

The access token is only sent to `https://cli-chat-proxy.grok.com` (port 443, no URL credentials or fragment, no redirects). Only a fingerprint of the account's `user_id` is cached; the token, refresh token and email are never stored, logged or printed. Errors go to `hook-errors.log` as a class name and HTTP status only.

Money: what is left under `onDemandCap` (minus `onDemandUsed`) plus `prepaidBalance` counts as paid credits. Once the period is used up such an account would be charged, so it is blocked unless the dispatch carries `QR_ALLOW_CREDITS=1`; with neither, a used-up period is simply "weekly quota used up".

Unknown quota: a login file that is missing or unreadable (also: Grok not installed, or a login only through `XAI_API_KEY`), an `expires_at` in the past, a non-200 answer, a timeout or an answer of another shape all leave the account dispatchable with unknown quota. The last numbers are kept and shown as "numbers are old: the last reading showed N% left"; a failure cools down like the other families.

Login renewal: quota-router never uses the refresh token, because a rotated refresh token written by another program could sign the CLI out. The CLI renews an expired login itself before its next request, under `auth.json.lock`, including for `grok models`, which costs no tokens. With `[families.grok] refresh_logins = true` an expired login makes a reading start `refresh-grok <key> <owner>` detached (output in `refresh-<key>.log`): it runs `grok models` with that account's `GROK_HOME` and then reads the quota once. `QR_NO_REFRESH=1` turns it off as well.
