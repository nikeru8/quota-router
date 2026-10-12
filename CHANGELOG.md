# Changelog

## Unreleased

### Added

- **macOS: the second login in `~/.claude`.** Claude Code keeps one keychain item per `CLAUDE_CONFIG_DIR` spelling, so a plain `claude` and `CLAUDE_CONFIG_DIR=~/.claude claude` can be two accounts in the same directory. On macOS `dir = "~/.claude"` written out is now a separate Claude account (dispatched to with the variable set) next to `dir = ""` (the variable unset); a value naming that directory in a command, in `CLAUDE_CONFIG_DIR` of the session or in the process list maps to the explicit account first. `qr doctor` warns when that second item holds a login the config does not list. Two accounts with the same directory are now a config error on every platform.
- **`qr status --bars`** prints a bar per quota window under each account (`▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱▱▱▱▱▱  43/100%`: weekly, five-hour and the scarce model's bucket for Claude; weekly for Codex). `--watch` shows the bars too.
- **`qr status --watch [N]`** redraws the status table every N seconds (default 10) until Ctrl-C. On a terminal the screen is cleared between draws; in a pipe the tables are separated by a dashed line. `--refresh` applies to the first draw only, so watching reuses cached numbers and does not add load on the usage endpoints.
- **Sessions start on the roomiest account.** `qr install` also writes a `claude` launcher (`~/.local/share/quota-router/bin/claude`, or under `$XDG_DATA_HOME`; Windows `claude.cmd` under `%LOCALAPPDATA%`, untested). With its directory first on `PATH`, `claude` typed in a terminal asks `qr launch` for the Claude account, by the hook's scoring, and starts the real claude there. There is no lead to protect at that point. Only accounts set up for sessions are candidates: `launch` not `false`, and the quota-router hook in that directory's `settings.json` (each directory has its own settings; quota-router does not copy them). Interactive sessions already running on an account count as jobs there, and an account whose quota can be read comes before one whose quota cannot. A resumed conversation starts on the account that holds it; `--continue` uses a record of the sessions started through the launcher in that directory, so it is not misled by a dispatch the hook sent elsewhere. A launch record only counts when that session wrote a conversation there (not a dispatch, changed no earlier than the launch), so a session quit without typing anything does not send the next `--continue` to a dispatch transcript; and an account `--continue` only inferred is never recorded. When stderr is a terminal, one line says where the session went and why.
- **`qr launch [--] <claude arguments>`**, the command behind the launcher. It never refuses to start a session: when nothing fits, nothing can be read, the config is broken, it fails or it takes too long, claude starts on the default account with a line saying why, and `⚠ PAID OVERAGE` when that account would bill. `--help`, `--version`, `--bg`, subcommands (`claude mcp …`), `claude -p …`, a `CLAUDE_CONFIG_DIR` already set and commands inside a Claude Code session pass through untouched.
- **The launcher never stands in the way.** When the Python it was installed with is gone, the package is removed or is an older version without `qr launch`, or quota-router fails, it starts the first other `claude` on `PATH` unchanged.
- `launch = false` in an `[[accounts]]` table keeps new sessions off that account. `[launch] route_print = true` routes `claude -p` started outside Claude Code as well.
- `qr install --no-launcher` installs the hook only; `qr uninstall` removes the launcher when it is exactly the file `qr install` wrote (`--no-launcher` keeps it). With `--config-dir`, neither touches the launcher. Neither edits a shell startup file; `qr install` prints the `PATH` line to add.
- `qr doctor` checks the launcher, runs `claude --version` through it, its place on `PATH`, `claude` aliases and functions in the shell startup files that would bypass it, accounts listed without a hook, and that some account can take new sessions. `qr status` and `qr config` name accounts passed over for missing the hook.
- `decisions.log` source `qr launch`, with kinds `launch`, `launch-resume` and `launch-default`.

### Fixed

- **macOS: the login at the default location could read as "not logged in".** A keychain service can hold two items, one with only MCP logins (account `unknown`) and one with the Claude login (account = your user name), and `security find-generic-password -s` returned the first. The item filed under your user name is now asked for first, then any item of the service, then `.credentials.json`; the first with a Claude login is used. This affects the hook, `qr status` and the launcher alike.

### Not changed

- The hook routes dispatches exactly as before; in particular it does not count interactive sessions as busy, and it treats an unreadable account as before. A session still runs on one account from start to end: subagents from the Agent tool and workflows are not moved mid-session.

### Known issues

Found in review, not fixed yet:

- `qr doctor` does not notice `CLAUDE_CONFIG_DIR` exported in a shell startup file or already in its environment; with it set, every `claude` passes through the launcher unrouted.
- `qr doctor` only reads the startup files it names; a `claude` alias or function in a file they `source` is not seen.
- `claude --continue` with two launcher-started sessions in the same directory on different accounts resumes the account started last, not the one with the newest conversation (claude's own `--continue` picks the newest conversation).
- Windows: a choice abandoned after the time limit keeps running in a background thread next to claude (on macOS and Linux claude replaces the process); and `claude.cmd` passes its arguments on with `%*`, which `cmd` re-parses, so arguments with `"`, `%`, `^` or `&` may arrive changed. The Windows launcher is untested on a real machine.

## 0.1.0

First public version. It is a port of a tool that has been routing subagents on one macOS machine, with everything specific to that machine moved into a config file.

### What it does

- **Routes subagent commands.** A Claude Code `PreToolUse` hook that, for every `claude -p …` and `codex exec …` the session is about to run, picks the account with the most room in the same family and puts `CLAUDE_CONFIG_DIR=…` / `CODEX_HOME=…` in front of the command.
- **Uses quota before it expires.** Accounts are ranked by weekly quota left per hour until the weekly reset, scaled down for a tight five-hour window and for jobs already running on the account. Equal scores go to the account listed first in the config file.
- **Protects the lead session.** The account the dispatching session runs on stops taking subagents below a threshold.
- **Blocks instead of failing halfway.** When no account in the family can take a dispatch, the whole command is refused with the reason for each account and the earliest scheduled reset.
- **Does not spend money unless told to.** Dispatches that the readings say would draw on purchased Codex credits or Claude paid overage are blocked, including on accounts named explicitly, on resumed conversations and in `qr pick` / `qr check`. `QR_ALLOW_CREDITS=1` as a prefix of one command lets that command through; `general.allow_credits = true` turns the check off.
- **Keeps resumed conversations where they are.** `claude -p … --resume <id>` and `codex exec resume <id>` stay on the account that holds the conversation.
- **Respects an account you name**, with a warning when it would have been skipped.
- **Reports the other family.** A dispatch carries a one-line summary of the other family's quota when that family is configured and could be read, with a hint when it has clearly more room. The family is never switched automatically.
- **Handles one per-model weekly bucket** (`families.claude.scarce_model`). Claude dispatches without `--model` are checked against it.
- **Notices resets from readings**, including early resets and unannounced refills, and tells the lead once per session.
- **Skips accounts nobody is logged in to.** An account whose quota merely cannot be read is still eligible, with a middle score; old numbers are used with a warning rather than blocking.
- **Fails open.** A missing or broken config, or an internal error, leaves the command unchanged. The hook aims to answer within eight seconds; a watchdog ends it two seconds later without a decision.

### Tooling

- `qr init`, `qr config`: create and inspect the config file (`~/.config/quota-router/config.toml`, or `%APPDATA%\quota-router\config.toml`).
- `qr install`, `qr uninstall`: add or remove the hook in a Claude Code `settings.json`, with a backup. `qr install` tries the installed command and exits with status 1 if it does not answer.
- `qr doctor`: one `OK` / `WARN` line per check of the setup.
- `qr status`, `qr report`, `qr decisions`: current quota, reset history and pace, past decisions.
- `qr pick`, `qr check`: the same selection, with the same credit rules, for use inside wrapper scripts.
- Messages in English and Traditional Chinese (`general.language`, `QR_LANG`).
- macOS, Linux and Windows (Git Bash). Python 3.11 or newer, no dependencies.

### Privacy

- Login tokens are read only, never refreshed or stored by quota-router, and sent only to the vendor's own usage endpoint. HTTP redirects are never followed.
- `decisions.log` and `hook-errors.log` hold structured fields only: no command text, prompt, token, email or exception message. Full tracebacks go to `debug.log` only with `QR_DEBUG=1`.

### Limits

- It relies on undocumented usage endpoints of both vendors, which may change without notice.
- The hook reads the command as text. Dispatches inside `bash -c '…'`, `eval`, scripts, the arguments of another program (`python3 wrap.py -- claude -p …`), or behind a leading redirection are not seen, and so are not checked for credits either. Whether an `export` or `unset` behind `&&` or inside `if` runs is not evaluated. A loop or `xargs` is seen as one dispatch. `--resume=ID` (with an equals sign), `codex exec <flags> resume ID`, and resuming without an id are not handled reliably. `$(…)/bin/claude` as the program name breaks the command.
- Only Claude Code's Bash tool is hooked. Subagents started through the built-in Agent/Task tool or a workflow are not routed.
- A Bash command is allowed or blocked as a whole.
- Windows: only Git Bash commands are handled, not Claude Code's PowerShell tool. Running jobs are counted only when the account is visible on a command line. The suite passes in CI on Windows, but nobody has used it there day to day.
- Only the weekly window, the five-hour window and one per-model weekly bucket are modelled.
- Not on PyPI yet; install from GitHub.

Details are in [docs/REFERENCE.md](docs/REFERENCE.md).
