# quota-router

[繁體中文](README.zh-TW.md)

quota-router is a Claude Code `PreToolUse` hook for people who run subagents on more than one subscription account. Each time your Claude Code session is about to run `claude -p …` or `codex exec …`, the hook looks up the quota of every account you listed (a live query, reused for a few minutes), picks the one with the most room in the same family (Claude or Codex), and puts that account's environment variable in front of the command. If no account can take the job, or the job would spend paid credits, the command is blocked with an explanation instead.

The session types:

```sh
claude -p "summarize the diff" --model opus
```

The hook turns it into:

```sh
env CLAUDE_CONFIG_DIR=/home/you/.claude-b claude -p "summarize the diff" --model opus
```

and adds a note the session can read:

```
[quota-router] Automatically routed to bob: weekly 88% left (resets in 4.0 d), 0 jobs running, 1 recently reserved
```

When nothing fits, the command does not run and the session gets the reason. Here both Codex accounts are out of subscription quota and one of them has purchased credits:

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

`alice`, `bob` and the account keys are made up. In messages an account is shown by the part of its login email before the `@`, or by its config key where the email is not known or a list of accounts is given.

The session itself, and everything it runs in its own process (the built-in Agent tool, workflows), stays on the account it was started on. For that, `qr install` also writes a `claude` launcher: put it first on `PATH`, and `claude` typed in a terminal starts on the Claude account with the most quota left. See [Launching sessions](#launching-sessions).

## Why

- **Several accounts, one session.** A subagent runs on whichever account its environment points at. Left alone, they all pile onto the account the lead session uses.
- **Weekly quota expires.** Each account's allowance resets on its own schedule and whatever is unused is gone. The account that resets soonest should be used first.
- **The lead session does not look.** It cannot see quota unless something shows it, and it will not check before every dispatch.
- **Running out can cost money without a word.** An account with purchased credits or paid overage enabled keeps working after the subscription quota is gone; it just starts charging.

## Requirements

- Python 3.11 or newer. No third-party packages.
- [Claude Code](https://claude.com/claude-code) as the lead session. This is a Claude Code hook; it does nothing on its own. `qr install` writes the hook in exec form (`command` plus `args`), so you need a Claude Code version that accepts that form. No minimum version has been established; step 5 of the install is the check.
- Optional: the Codex CLI, if you also dispatch `codex exec`.
- macOS, Linux or Windows. On Windows, [Git for Windows](https://git-scm.com/download/win) is required: Claude Code's Bash tool runs in Git Bash, and Bash commands are all this hook sees.
- Two or more logged-in accounts in a family, each in its own directory, or there is nothing to route between. For an extra account: `CLAUDE_CONFIG_DIR=~/.claude-b claude` (or `CODEX_HOME=~/.codex-b codex`) and log in there once. Each directory is a separate CLI configuration: models, permissions, instructions and integrations are whatever that directory holds. quota-router does not synchronize them, so a routed job runs with the settings of the account it lands on.

## Install

Until it is on PyPI, install from GitHub:

```sh
pip install git+https://github.com/nikeru8/quota-router
```

On a Python that refuses to install packages directly (Homebrew's on macOS, Debian's, most distro Pythons; the error says "externally-managed-environment"), or when there is no `pip` command, install into a virtual environment instead:

```sh
python3 -m venv ~/.qr-venv
~/.qr-venv/bin/pip install git+https://github.com/nikeru8/quota-router
~/.qr-venv/bin/qr --help
```

Then either call `~/.qr-venv/bin/qr` by that path or add `~/.qr-venv/bin` to your `PATH` (`pipx install git+https://github.com/nikeru8/quota-router` does the same in one step if you have pipx). `qr` does not have to be on Claude Code's `PATH`: the hook is installed with the absolute path of the Python it runs on.

1. **Create the config file.**

   ```sh
   qr init        # writes the file and lists the accounts it found
   ```

   It pre-fills the default location of each CLI plus every `~/.claude-*` and `~/.codex-*` directory that looks like an account. It only checks which files exist; it reads none of them. **Being listed does not mean being logged in**: the two default locations are always listed, even on a machine that has never used that CLI.

2. **Edit it.** Open the path `qr init` printed and make the `[[accounts]]` list match your logins (see [Configuration](#configuration)). Then check what the tool sees, and delete every account that `qr status` shows without a reading because nobody is logged in there:

   ```sh
   qr config      # every setting, its value and where it came from
   qr status      # one row per account with its current quota
   ```

3. **Add the hook** to every Claude Code account you dispatch from. Each account directory has its own `settings.json`:

   ```sh
   qr install                              # ~/.claude/settings.json
   qr install --config-dir ~/.claude-b     # once more for each other Claude account
   ```

   This adds one `PreToolUse` entry for `Bash` and leaves the rest of the file as it was. The previous file is saved next to it as `settings.json.bak-<time>`. Running it again changes nothing. It then runs the installed command once with a harmless test event and exits with status 1 if that command does not answer. A successful install means the hook starts; it does not mean routing is configured, since the hook also answers the test without a config file.

   Without `--config-dir` it also writes the `claude` launcher to `~/.local/share/quota-router/bin/claude` (see [Launching sessions](#launching-sessions)) and, when that directory is not yet ahead of the real `claude` on your `PATH`, prints the line to add to `~/.zshrc` or `~/.bashrc`. It never edits those files itself. `--no-launcher` installs the hook only; `--config-dir` never touches the launcher. New sessions only land on account directories that have the hook, so run the `--config-dir` form for every account you want sessions on.

4. **Check the setup.**

   ```sh
   qr doctor      # one OK or WARN line per check
   ```

   Read the lines: `qr doctor` exits with status 0 even when it prints warnings.

5. **Try it.** Start a new Claude Code session and ask it to run

   ```sh
   claude -p "say hi" --model sonnet
   ```

   The tool result should carry a line starting with `[quota-router] Automatically routed to`. `qr decisions` lists what was decided and why.

   With the launcher's directory on `PATH`, open a new terminal and run `claude`: before the session starts, it prints one line starting with `[quota-router] session on`.

To run from a checkout without installing: `git clone`, then `bin/qr <command>` (`python bin\qr <command>` on Windows).

## Configuration

The config file is `$QR_CONFIG` if set, otherwise `~/.config/quota-router/config.toml` (Windows: `%APPDATA%\quota-router\config.toml`). Only `[[accounts]]` is required.

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
| `launch` | `true` | Claude accounts only. `false`: [`qr launch`](#launching-sessions) never starts a new session here. Even with `true`, a session only starts here once the hook is in this directory's `settings.json`. Dispatches from the hook still go to the account either way, and a conversation that lives there is still resumed there. |

The order of the accounts matters in three cases: when two accounts score exactly the same, the one listed first is used; when two Claude directories are the same account, the first one that is not the lead's represents it; and when the time budget cannot cover every account, accounts with equally old readings are queried in this order.

Two Claude directories logged in to the same account (same email) are treated as one account: one quota, busy counts added together.

A family with no accounts listed is not managed at all. Its dispatches are neither routed nor checked, so it has no credit protection either.

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
| `timing.hook_budget_seconds` | `8` | Time the hook may spend on one command. Two seconds past it, the hook process is ended and the command runs unchanged. |
| `timing.codex_stale_hours` | `2` | Codex numbers that are not live and older than this are treated as unknown. |
| `timing.codex_probe_hours` | `2` | Minimum gap between background Codex probes per account. |
| `timing.notice_ttl_hours` | `6` | A reset notice is shown for this long. |
| `events.reset_drop_points` | `3` | Usage must drop by this many points to count as a reset or refill. |
| `families.claude.scarce_model` | `"fable"` | The Claude model that has its own weekly bucket. `""` turns the per-model check off. |
| `launch.route_print` | `false` | `true`: the [launcher](#launching-sessions) also chooses the account for `claude -p …` started outside Claude Code. By default those run as written. |
| `endpoints.claude_usage` | `"https://api.anthropic.com/api/oauth/usage"` | Where Claude usage is queried. |
| `endpoints.claude_profile` | `"https://api.anthropic.com/api/oauth/profile"` | Where a Claude account's email is queried. |
| `endpoints.codex_usage` | `"https://chatgpt.com/backend-api/wham/usage"` | Where Codex usage is queried. |

`[endpoints]` exists for tests and for networks that reach the vendors through a proxy. **Your login token is sent to whatever these point at**; leave them alone otherwise. `qr init` writes them commented out.

Unknown sections, settings and account fields are ignored with a warning, which `qr config` and `qr doctor` print; the hook and the other commands do not repeat it. A wrong type, an unknown language, a duplicate key or an empty account list is an error: the commands that need the config (`status`, `pick`, `check`, `report`, `config`, …) exit with status 2, `qr doctor` reports it as a `WARN` line and exits 0, and the hook lets commands through unchanged with one line on stderr. [docs/REFERENCE.md](docs/REFERENCE.md) has the full validation rules.

### Environment variables

| Variable | Effect |
|---|---|
| `QR_CONFIG` | Path of the config file. |
| `QR_STATE` | State directory. Default `~/.local/state/quota-router` (or under `$XDG_STATE_HOME`); Windows `%LOCALAPPDATA%\quota-router`. |
| `QR_LANG` | `en` or `zh-TW`; overrides the config file. |
| `QR_ALLOW_CREDITS=1` | As a prefix of one dispatch (`QR_ALLOW_CREDITS=1 codex exec …`), lets that dispatch spend credits. `export` does not count for the hook; `qr pick` / `qr check` read it from the environment. |
| `QR_NO_PROBE=1` | Never start the background Codex probe (see [Privacy and security](#privacy-and-security)). |
| `QR_DEBUG=1` | Also write full tracebacks to `debug.log` in the state directory. That file can contain sensitive text. |

### Commands

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
qr pick <family>                     print the best account as an env prefix and reserve it
                                     (--env, --peek, --model <name>; --strict is accepted and ignored)
qr check <family> <key|number|dir>   check one specific account
qr report [--days N]                 reset events, current pace, dispatch statistics
qr decisions [N]                     the last N routing decisions
qr leases --clear                    drop all reservations
```

The hook reads the text of the Bash command and nothing else. A dispatch that happens inside a script the command runs is not in that text, so the script has to ask for an account itself. Exit status 0 prints the prefix; exit status 3 prints nothing on stdout and explains on stderr. `qr pick` never returns an account that would spend credits unless `QR_ALLOW_CREDITS=1` is in its environment:

```sh
account=$(qr pick codex --env) || exit 3
eval "env $account codex exec \"\$prompt\""
```

The `eval` is there because the directory is shell-quoted when it contains a space or another special character.

## How it decides

- **Same family only.** Claude or Codex is your choice. The hook picks the account and never switches family.
- **Readings.** Each account's quota is queried live and the answer is reused for `usage_cache_seconds`. No query is made while an endpoint that just failed is in back-off, while another hook process is already asking, or when the time budget is nearly used up; the last known numbers are used then.
- **Score.** Weekly % left divided by hours until the weekly reset, so quota that is about to expire is used first. A five-hour window under 30% scales the score down. The result is divided by 1 + the number of jobs on that account: running `claude -p` / `codex exec` processes or dispatches made in the last five minutes, whichever is larger. The highest score wins; a tie goes to the account listed first in the config file. Once spending is allowed, accounts that would not spend come before accounts that would, whatever their score.
- **Accounts that are skipped.** Five-hour window under `five_hour_floor`; weekly quota used up; for accounts other than the lead's, weekly quota under `weekly_floor`; no login credentials in the account's directory.
- **Accounts whose quota cannot be read are not skipped.** They get the middle score of the accounts that could be read, and the note says the quota is unknown. So an account you listed but cannot be queried (endpoint down, token expired) can still receive a job.
- **Old numbers are used rather than nothing.** When every account is blocked, readings that are not fresh are queried again. If an account's numbers are still old after that, it is not the lead's and it could not cost money, the job is sent there with a `⚠` warning that the numbers are old.
- **The lead is protected.** The account your session itself runs on is not picked once it is below `protect_threshold`, or when its quota cannot be read. This applies to automatic selection: naming that account yourself, or resuming a conversation that lives on it, still uses it.
- **Nothing fits: block.** The whole Bash command is refused, with the reason for each account, the earliest scheduled reset and, when it could be read, the state of the other family.
- **Money is off by default.** A dispatch that the readings say would draw on purchased Codex credits or Claude paid overage is always blocked: on automatic routing, on an account you named, on a resumed conversation, and in `qr pick` / `qr check`. Spending is allowed only by `QR_ALLOW_CREDITS=1` written as a prefix of that command, or for everything by `general.allow_credits = true`. The limits of this check: it relies on what the readings show, so it passes, with a note, when the payment state is unknown (an account directory that is not in the config file, a resumed conversation whose account cannot be found, a reading without credit information); and it cannot stop a job that is already running from using up its quota and continuing on paid usage.
- **Resumed conversations stay put.** `claude -p … --resume <id>` and `codex exec resume <id>` go to the account that holds the conversation. If it is out of quota, the command is blocked and you are told to dispatch again elsewhere with a summary.
- **You named the account: it is used.** `CLAUDE_CONFIG_DIR=… claude -p …` and `CODEX_HOME=… codex exec …` run as written, with a warning if that account would have been skipped. If the variable is cleared again before the dispatch (`unset`, `env -i`, `env -u CODEX_HOME`), no account is named and the dispatch is routed normally. One spelling is an exception: `env -u CLAUDE_CONFIG_DIR claude -p …` is how you name the Claude account at the default location, and it is taken as that.
- **The other family is a suggestion.** A dispatch gets a one-line summary of the other family when that family has accounts and could be read in time, and a 💡 line when it has clearly more room. A command with several dispatches gets the summary for its first one. The lead decides.
- **One model can have its own bucket.** With `scarce_model` set, a Claude dispatch that names that model, or names none, is also checked against that model's weekly bucket. A resumed conversation without `--model` keeps its account as long as the shared quota has room, but is checked against that bucket for paid overage. If the usage endpoint does not report the bucket, there is nothing to check.
- **Resets are read, not assumed.** When a live reading shows usage dropping by at least `reset_drop_points`, it is recorded as a reset or refill and the lead is told once per session. An ordinary five-hour rollover on schedule is not announced, and a reset date that merely moved is recorded for `qr report` without a notice.
- **Fail open.** If quota-router itself breaks, the command runs as written. The hook aims to answer within `hook_budget_seconds`; two seconds after that a watchdog ends it without a decision. The time Python takes to start is on top.

## Launching sessions

The hook only sees the `claude -p` / `codex exec` commands a session runs through its Bash tool. Everything a session does in its own process (the built-in Agent tool, workflows) runs on the account the session was started on. The launcher chooses that account when the session starts.

`qr install` writes a small script named `claude` into `~/.local/share/quota-router/bin/` (`$XDG_DATA_HOME/quota-router/bin/` when that is set; Windows: `%LOCALAPPDATA%\quota-router\bin\claude.cmd`). Put that directory first on `PATH`, in `~/.zshrc` or `~/.bashrc`:

```sh
export PATH="$HOME/.local/share/quota-router/bin:$PATH"
```

From then on `claude` in a terminal asks `qr launch` which account to use, then starts the real `claude` with that account's `CLAUDE_CONFIG_DIR` (or with it unset, for the account at the default location). The real `claude` is looked up on `PATH` at every start, past the launcher's directory, so updating or moving it needs nothing. When stderr is a terminal, one line says where the session went:

```
[quota-router] session on bob: weekly 88% left (resets in 4.0 d)
```

**Which accounts can take a session.** An account is used for new sessions only when it is *set up* for them: `launch` is not `false`, and `qr install` has put the hook into that directory's `settings.json` (`qr install --config-dir ~/.claude-b`). Each account directory carries its own settings: the hook, permissions, MCP servers, model, output style and `CLAUDE.md`. quota-router does not copy them from one directory to another, so a session lands only where you have set things up. `qr status`, `qr config` and `qr doctor` name the accounts that are passed over because the hook is missing.

How it chooses:

- **A new session** goes to the best account that is set up, by the hook's rules: the same score (weekly % left per hour to the reset, scaled for a tight five-hour window and for jobs running), the same skips (five-hour floor, weekly floor, weekly quota used up, not logged in, paid overage unless allowed), and a reservation as with `qr pick`. Three differences from a dispatch: there is no lead yet, so no account is held back as the lead's; interactive sessions already running on an account count as jobs there; and an account whose quota can be read comes before one whose quota cannot. Without `--model`, the scarce model's bucket counts too, as for a dispatch.
- **A resumed conversation** (`--resume <id>`, `-r <id>`, `--resume=<id>`) starts on the account that holds it. `--continue` / `-c`, `--resume` without an id (the picker) and `--from-pr` go to the account of the last session started through the launcher in this directory; without one, to the newest conversation of this directory that is not a `claude -p` run. This also holds for an account that is not set up for sessions or has no quota left, with a `⚠` in the line. When the account cannot be found, the session starts on the default account.
- **Left alone, nothing printed:** `CLAUDE_CONFIG_DIR` already set (you, or the hook, chose); a command run inside a Claude Code session (`CLAUDECODE` set: the hook handles those); `claude -p …` (scripts and `qr pick` write the default account as `env -u CLAUDE_CONFIG_DIR claude -p …`; set `[launch] route_print = true` to route one-shot runs started outside Claude Code as well); `--help`, `-h`, `--version`, `-v`, `--bg`; a subcommand: `mcp`, `config`, `doctor`, `update`, `install`, `auth`, `plugin`, `setup-token`, `migrate-installer`, and any other bare lower-case word in first position. So `claude hello` starts unrouted, `claude "hello there"` is routed.
- **It never refuses.** When every account is blocked, no account's quota can be read, the config file is missing or broken, quota-router fails, or choosing takes more than `hook_budget_seconds` plus two seconds, the session starts on the default account and the line says why. An account that would bill paid overage is never chosen automatically. When a session lands on one anyway (a resumed conversation, the default account as fallback, or with `QR_ALLOW_CREDITS=1` / `allow_credits = true`), the line says `⚠ PAID OVERAGE`.
- **It never stands in the way.** When quota-router itself cannot run (the Python it was installed with is gone, the package was removed or is an older version without `qr launch`, or it fails), the launcher starts the first other `claude` on `PATH` unchanged. `qr doctor` runs `claude --version` through the launcher to check that it starts claude.

What it does not do:

- **It does not move a running session. One session = one account.** Subagents from the Agent tool and workflows stay on the session's account however low it gets; start a new session to use another one.
- It does not edit `~/.zshrc` or any other startup file. `qr doctor` points out a `claude` alias or function that gets in the way. An alias that runs `claude` itself (`alias claude='claude --dangerously-skip-permissions'`) still goes through the launcher, because the shell looks the `claude` inside an alias up on `PATH`. An alias that runs an absolute path, or that sets `CLAUDE_CONFIG_DIR`, bypasses it.
- It is not free: every `claude` started this way first starts Python (about a tenth of a second), and reading quota that is not fresh can take up to `hook_budget_seconds`. Dispatches inside a session also pass through the launcher when it is first on that session's `PATH`; it steps aside at once.

`qr launch [--] <claude arguments>` does the same without the launcher. Its decisions are in `decisions.log` with the source `qr launch`.

## Messages you will see

All of them go to the lead session, attached to the Bash tool call.

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

Blocked because it would cost money: see the example at the top.

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

The rule behind all of these: **what the hook cannot see, it does not route, and it does not check for credits either.** It reads the text of a Bash command before the command runs. It does not run a shell, expand variables or look inside scripts. A dispatch counts only when `claude` / `codex` stands where the shell looks for a command, possibly behind a known launcher (`env`, `timeout`, `nice`, `nohup`, `time`, `command`, `exec`, `sudo`, `caffeinate`, `stdbuf`, `setsid`, `xargs`, `parallel`, `find -exec`).

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

- Only Claude Code's Bash tool is hooked. Subagents started with the built-in Agent/Task tool or a workflow run on the lead's own account; the [launcher](#launching-sessions) chooses that account when the session starts, and nothing changes it afterwards. A dispatch made inside a script is not in the command text, so it is neither routed nor checked (use `qr pick` in the script).
- Text that is only data is left alone: quoted strings, comments and heredoc bodies, whatever the delimiter. A dispatch written inside a heredoc that is fed to a shell (`bash <<EOF … EOF`) is therefore not seen either.
- A Bash command is allowed or blocked as a whole. In `codex exec a; claude -p b`, a blocked Claude dispatch stops the Codex part too.
- Only the weekly window, the five-hour window and one per-model weekly bucket are modelled.
- `qr install` rewrites `settings.json` as standard JSON. Contents, key order and indentation width are kept; hand-aligned or single-line objects are re-wrapped. The backup is the untouched original.
- An account whose directory name contains a space is not credited with its running jobs on macOS and Linux (the process environment cannot be split reliably there); reservations still count.
- `qr status` and the summary line show the age of the numbers. "live" means read within the last few minutes, not necessarily in this very call.
- `qr check codex <number>` means "the n-th Codex account in the config file", so it changes when you reorder the file. Use keys in scripts.

## Platform notes

| | macOS | Linux | Windows |
|---|---|---|---|
| Config file | `~/.config/quota-router/config.toml` | same | `%APPDATA%\quota-router\config.toml` |
| State directory | `~/.local/state/quota-router` (or `$XDG_STATE_HOME`) | same | `%LOCALAPPDATA%\quota-router` |
| Claude login read from | keychain, else `<dir>/.credentials.json` | `<dir>/.credentials.json` | `<dir>\.credentials.json` |
| Codex login read from | `<dir>/auth.json` | same | same |
| Running jobs counted from | `ps`, including each job's environment | same | PowerShell `Get-CimInstance Win32_Process`, command lines only |
| Shell the command runs in | `sh` / `zsh` | `sh` / `bash` | Git Bash |
| `claude` launcher | `~/.local/share/quota-router/bin/claude` (or under `$XDG_DATA_HOME`) | same | `%LOCALAPPDATA%\quota-router\bin\claude.cmd`, untested |

The test suite runs on all three in CI. Day-to-day use so far has been on macOS; treat Windows as the least proven.

- **macOS.** The first keychain read may bring up a system permission prompt.
- **Windows: Git Bash only.** Commands that Claude Code runs through its PowerShell tool are never routed.
- **Windows: the busy count is weaker.** Windows does not let one process read another's environment, so a running job is only attributed to an account when `CLAUDE_CONFIG_DIR=…` / `CODEX_HOME=…` is visible on its command line or its parent's. Jobs the hook routed to a named directory qualify. A job that got its account from an earlier `export` does not, and neither does a Claude job routed to the default account, because that one is written as `env -u CLAUDE_CONFIG_DIR` and names no directory. Reservations from the last five minutes are counted the same on every platform.
- **Windows: listing processes starts PowerShell**, which takes roughly 0.5–2 s of the hook's budget. Past 2 s nobody is counted as busy.
- **Windows: paths** are written into commands with forward slashes (`C:/Users/me/.claude-b`). In your own commands and in the config file, `C:\…`, `C:/…`, `~/…` and the Git Bash spelling `/c/…` are all understood; the config loader converts `/c/…` to a drive path on Windows.
- **Windows: logins kept elsewhere are not read.** That includes a Console sign-in under `%APPDATA%\Anthropic` and a Codex login kept in the system credential store.
- **Windows: the launcher is untested.** `qr install` writes a batch file, `claude.cmd`, which runs `qr launch`; there `claude` runs as a child process instead of replacing it. The logic is covered by the test suite, but the file has not been run on a real Windows machine.

## Privacy and security

**What it reads**, all read-only:

- The Claude Code OAuth token: on macOS from the login keychain through `security find-generic-password`, otherwise from `<dir>/.credentials.json`. One keychain service can hold more than one item (one with only MCP logins, one with the Claude login), so the item filed under your user name is asked for first, then any item of that service, then the file.
- The Codex token in `<dir>/auth.json`.
- As fallbacks when a query fails: the usage Claude Code cached in `.claude.json`, and the last `rate_limits` record in Codex's `<dir>/sessions/**.jsonl`. To find that record up to 20 of the most recent session files are read line by line; only the record is kept.
- The names and modification times of conversation files in the account directories, to find which account holds a resumed conversation. For `claude --continue` through the launcher, also the first lines of those files in the current directory's project folder, only to tell a `claude -p` run or a subagent from an interactive session.
- The process list, to count running `claude` / `codex` jobs per account. That means the command lines of running processes, which can contain prompt text, and on macOS and Linux the environment of the `claude` / `codex` ones. Only the program name and `CLAUDE_CONFIG_DIR` / `CODEX_HOME` are used; none of it is stored.
- `qr doctor` only: your shell startup files (`~/.zshrc`, `~/.bashrc` and a few more), to find a `claude` alias or function. Only those lines are shown, with anything that looks like a credential masked; nothing is stored.

quota-router itself never refreshes, rewrites or copies a token.

**Where it sends anything.** Each token goes to its own vendor's usage endpoint and nowhere else. HTTP redirects are never followed; a redirect counts as a failed query:

- `https://api.anthropic.com/api/oauth/usage`
- `https://api.anthropic.com/api/oauth/profile`
- `https://chatgpt.com/backend-api/wham/usage`

quota-router itself sends no telemetry and makes no other request, unless you change `[endpoints]`. One side effect to know about: when a live Codex query fails and the local numbers are over two hours old, one `codex exec` no-op ("Reply with the single word: ok", read-only sandbox, low reasoning effort) is started in the background to refresh them. That is the real Codex CLI running on that account: it talks to its vendor, may refresh its own login and writes a session file, all outside the guarantees above. It uses the model that account's Codex config selects, so what it costs depends on that config; it is small but not zero. It is not started when the account is known to have credits and its subscription quota is used up, nearly used up or unknown; when the credit balance itself is unknown, it is. It happens at most once per account per two hours, and `QR_NO_PROBE=1` turns it off.

**What it writes**, all in the state directory:

- State files: a usage cache, reservations, usage and reset history, a record of what `qr install` installed, and for `--continue` the sessions started through the launcher (a hash of the working directory, the account key and the time; never the path). These hold account emails, short hashes of tokens (never the tokens) and the session ids of recent reservations.
- `decisions.log`: one line per decision with the time, the lead's account key, the first 8 characters of the session id (or a hash of it), the chosen account key, family, kind of decision, quota numbers, model class and a 12-character hash of the command. No command text, no prompt, no email, no directory.
- `hook-errors.log`: one line per error with the time, operation name, exception class, code location and the account key or state file concerned. No exception message and no traceback, because those can quote headers and response bodies.
- `probe-<account>.log`: whatever the `codex` CLI printed during a background probe.
- `*.lock` files and, briefly, `*.tmp` files.

Outside the state directory it writes only what `qr install` adds: the hook entry in each `settings.json` you install into, and the `claude` launcher file.
- `debug.log`, **only with `QR_DEBUG=1`**: full tracebacks. A pattern-based scrub is applied, but it is best effort; this file can contain tokens, headers, response bodies and config values. Treat it as sensitive and delete it when you are done.

The field-by-field format of both logs is in [docs/REFERENCE.md](docs/REFERENCE.md).

**The hook rewrites commands.** That is its job, and it is also the risk. A program that runs before every Bash command and can change it is a privileged thing to install. What it does is narrow: it inserts an environment prefix in front of a `claude` or `codex` invocation, or blocks the whole command; it changes nothing else in the command. A bug could still send a job to the wrong account or break a command it misreads (see [Known limitations](#known-limitations)). Read `qr decisions` after your first few dispatches. Anyone who can edit the config file can add accounts or redirect `[endpoints]`, and with them your tokens, so protect it like `settings.json`.

## Disclaimer

- quota-router uses **undocumented usage endpoints** of both vendors. They can change or disappear without notice, and the tool will then stop reading quota until it is updated.
- It is **not affiliated with, endorsed by or supported by Anthropic or OpenAI**.
- Whether using several subscription accounts side by side fits each vendor's terms is for you to check.
- **Use it at your own risk.** It is provided as is, without warranty (see [LICENSE](LICENSE)).

## Uninstall

```sh
qr uninstall                              # and once per --config-dir you installed into
pip uninstall quota-router
```

`qr uninstall` removes the entry `qr install` wrote, recognised by its exact command and argument list, and nothing else; entries written by hand, by another copy, or that merely mention this package are left alone. Without `--config-dir` it also removes the `claude` launcher when the file is exactly what `qr install` wrote (`--no-launcher` keeps it; `--config-dir` never touches it). The rules are in [docs/REFERENCE.md](docs/REFERENCE.md). The config file, the state directory, the launcher's empty directory and the `PATH` line in your shell startup file stay behind for you to delete.

## FAQ

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

The tests use a throwaway home directory and fake the network, keychain and process table; they never read real account data. `tests/test_fresh_machine.py` walks through the install in a new virtual environment against a local stand-in for the usage endpoints. It is skipped on Windows and where no cached `setuptools` is available offline (`QR_E2E_ONLINE=1` lets it download), and it launches the hook command itself: it does not show that a Claude Code session accepts the entry. [docs/REFERENCE.md](docs/REFERENCE.md) lists the skipped tests, the log formats and the other details; [CHANGELOG.md](CHANGELOG.md) says what each version contains.

## License

MIT, see [LICENSE](LICENSE).
