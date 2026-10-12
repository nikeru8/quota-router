# quota-router

[繁體中文](README.zh-TW.md)

quota-router is a Claude Code hook that spreads subagent jobs over your subscription accounts. When your session is about to run `claude -p "summarize the diff" --model opus`, the hook checks the quota of every account you listed and runs `env CLAUDE_CONFIG_DIR=/home/you/.claude-b claude -p "summarize the diff" --model opus` instead, on the account with the most room; when no account fits, or the job would spend paid credits, it blocks the command and says why. It acts only on `claude -p` and `codex exec` commands that a Claude Code session runs through its Bash tool. The session itself stays on its one account, and subagents started with the built-in Agent/Task tool or a workflow stay there with it.

The session sees a note like this on each routed command:

```
[quota-router] Automatically routed to bob: weekly 88% left (resets in 4.0 d), 0 jobs running, 1 recently reserved
```

Three terms used throughout:

- **lead**: the Claude Code session that issues the command. "The lead's account" is the account it runs on.
- **dispatch**: a `claude -p …` or `codex exec …` command that the hook sees in a Bash command.
- **family**: Claude or Codex. A dispatch is only ever routed to another account of the same family.

## Requirements

- Python 3.11 or newer. No third-party packages.
- Claude Code as the lead, in a version that accepts exec-form hooks (`command` plus `args`). Install step 5 is the check.
- At least two logged-in accounts in a family, each in its own directory: run `CLAUDE_CONFIG_DIR=~/.claude-b claude` (or `CODEX_HOME=~/.codex-b codex`) and log in there once.
- Optional: the Codex CLI, if you also dispatch `codex exec`.
- macOS, Linux, or Windows with [Git for Windows](https://git-scm.com/download/win) (Claude Code's Bash tool runs in Git Bash there).

## Install

Install the package from GitHub (it is not on PyPI yet):

```sh
pip install git+https://github.com/nikeru8/quota-router
```

Done when `qr --help` prints the command list.
On Homebrew, Debian and most distro Pythons ("externally-managed-environment"), or without `pip`: `python3 -m venv ~/.qr-venv && ~/.qr-venv/bin/pip install git+https://github.com/nikeru8/quota-router`, then call `~/.qr-venv/bin/qr` or add `~/.qr-venv/bin` to your `PATH`.

1. **Create the config file.**

   ```sh
   qr init
   ```

   Done when it prints the path of the new file and the accounts it pre-filled.

2. **Edit the `[[accounts]]` list** in that file to match your logins, then check what the tool sees:

   ```sh
   qr config
   qr status
   ```

   Done when every account in the file shows a quota reading in `qr status`. Keep only accounts that have one.

3. **Add the hook** to every Claude Code account you dispatch from:

   ```sh
   qr install                              # ~/.claude/settings.json
   qr install --config-dir ~/.claude-b     # once more for each other Claude account
   ```

   Done when it exits with status 0; the previous file is kept as `settings.json.bak-<time>`.

4. **Check the setup.**

   ```sh
   qr doctor
   ```

   Done when every line starts with `OK`. It exits with status 0 even when it prints `WARN` lines, so read them.

5. **Try it.** Start a new Claude Code session and ask it to run:

   ```sh
   claude -p "say hi" --model sonnet
   ```

   Done when the tool result carries a line starting with `[quota-router] Automatically routed to`. `qr decisions` shows what was decided and why.

## How it decides

- **Same family only.** You choose Claude or Codex; the hook chooses the account. The other family's quota is shown as a suggestion.
- **Score.** Weekly % left divided by hours until the weekly reset, so quota about to expire is used first. A five-hour window under 30% scales the score down. Busy accounts are divided by 1 + their jobs (running dispatches or ones sent in the last five minutes). Ties go to the account listed first.
- **Skipped accounts.** Five-hour window under `five_hour_floor`; weekly quota used up; weekly quota under `weekly_floor` (accounts other than the lead's); nobody logged in. An account whose quota cannot be read stays in, with the median score of the readable ones.
- **Lead protection.** The lead's account is left out of automatic picks once its tightest window is below `protect_threshold`, or when its quota cannot be read.
- **Nothing fits: block.** The whole Bash command is refused, with the reason for each account and the earliest scheduled reset.
- **Money is off by default.** A dispatch that the readings say would draw on purchased Codex credits or Claude paid overage is blocked everywhere: automatic routing, a named account, a resumed conversation, `qr pick` and `qr check`. To allow it for one command, write `QR_ALLOW_CREDITS=1` as a prefix of that one command (`QR_ALLOW_CREDITS=1 codex exec …`; `export` does not count). `general.allow_credits = true` allows it for everything.
- **Resumed conversations stay put.** `claude -p … --resume <id>` and `codex exec resume <id>` go to the account that holds the conversation.
- **Naming an account is honored.** `CLAUDE_CONFIG_DIR=… claude -p …` and `CODEX_HOME=… codex exec …` run as written, with a warning when that account would have been skipped.
- **Fail open.** When quota-router itself fails, or runs past its time budget, the command runs as written.

The full rules, with every exception, are in [REFERENCE.md › How it decides](docs/REFERENCE.md#how-it-decides).

## What it reads, sends and changes

- It reads each account's login token, read-only, and sends it only to that vendor's own usage endpoints. It never refreshes, rewrites or copies a token, and sends no telemetry.
- When a live Codex query fails and the local numbers are over two hours old, it starts one small `codex exec` in the background on that account to refresh them. `QR_NO_PROBE=1` turns that off.
- The hook runs before every Bash command and can rewrite it. It only inserts an account prefix or blocks the command. Read `qr decisions` after your first few dispatches.
- Anyone who can edit the config file can redirect your tokens, so protect it like `settings.json`.

## Limits that bite in practice

- **Resume with `=`.** `--resume=ID` is treated as a new dispatch and may move to an account without that conversation. Write `--resume ID`.
- **Hidden dispatches.** A dispatch inside `bash -c '…'`, `eval`, a script, or a heredoc fed to a shell is not seen: it is neither routed nor checked for credits. Call `qr pick <family> --env` inside the script.
- **Loops land on one account.** A `for` loop or `xargs` counts as one dispatch, so the whole batch goes to one account. Call `qr pick` inside the loop.
- **Only the Bash tool is hooked.** Agent/Task-tool and workflow subagents run on the lead's account.
- **Windows: Git Bash only, least proven.** Commands run through Claude Code's PowerShell tool are not routed, and running jobs are counted less reliably. The tests run on Windows in CI; day-to-day use so far has been on macOS.

The full table, with the fix for each case: [REFERENCE.md › Known limitations](docs/REFERENCE.md#known-limitations).

## Where to look next

- Accounts, settings and environment variables → [REFERENCE.md › Configuration](docs/REFERENCE.md#configuration)
- What each install step does and why → [REFERENCE.md › Install notes](docs/REFERENCE.md#install-notes)
- Every command, and using `qr pick` in scripts → [REFERENCE.md › Commands](docs/REFERENCE.md#commands) (also `qr --help`)
- Every message and what it means → [REFERENCE.md › Messages you will see](docs/REFERENCE.md#messages-you-will-see)
- Privacy: what is read, sent and logged → [REFERENCE.md › Privacy and security](docs/REFERENCE.md#privacy-and-security)
- macOS, Linux and Windows differences → [REFERENCE.md › Platform notes](docs/REFERENCE.md#platform-notes)
- Removing it → [REFERENCE.md › Uninstall](docs/REFERENCE.md#uninstall)
- Why it works the way it does → [REFERENCE.md › FAQ](docs/REFERENCE.md#faq)
- Tests and running from a checkout → [REFERENCE.md › Development](docs/REFERENCE.md#development)
- What each version contains → [CHANGELOG.md](CHANGELOG.md)

## Disclaimer

quota-router reads **undocumented usage endpoints** of Anthropic and OpenAI; they can change or disappear without notice, and the tool then stops reading quota until it is updated.
It is **not affiliated with, endorsed by or supported by Anthropic or OpenAI**. Whether running several subscription accounts side by side fits each vendor's terms is for you to check.
**Use it at your own risk.** It is provided as is, without warranty.

## License

MIT, see [LICENSE](LICENSE).
