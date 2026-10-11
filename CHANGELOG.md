# Changelog

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
