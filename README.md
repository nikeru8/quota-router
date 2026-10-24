# quota-router

[繁體中文](README.zh-TW.md)

quota-router is a Claude Code hook that spreads subagent jobs over your subscription accounts. When your session is about to run `claude -p "summarize the diff" --model opus`, the hook checks the quota of every account you listed and runs `env CLAUDE_CONFIG_DIR=/home/you/.claude-b claude -p "summarize the diff" --model opus` instead, on the account with the most room; when no account fits, or the job would spend paid credits, it blocks the command and says why. Inside a session it acts on `claude -p`, `codex exec`, `agy -p` and `kimi -p` commands run through the Bash tool. `qr install` also writes a `claude` launcher, so a session typed in a terminal starts on the Claude account with the most quota left. One session = one account: a session is never moved while it runs, and subagents started with the built-in Agent/Task tool or a workflow stay on its account.

The session sees a note like this on each routed command:

```
[quota-router] Automatically routed to bob: weekly 88% left (resets in 4.0 d), 0 jobs running, 1 recently reserved
```

Three terms used throughout:

- **lead**: the Claude Code session that issues the command. "The lead's account" is the account it runs on.
- **dispatch**: a `claude -p …`, `codex exec …`, `agy -p …` or `kimi -p …` command that the hook sees in a Bash command.
- **family**: Claude, Codex, agy (Antigravity), or Kimi Code. A dispatch is only ever routed to another account of the same family.

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

3. **Add the hook** to every Claude Code account you dispatch from or want sessions on. The first command also writes the `claude` launcher:

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

6. **Start sessions through the launcher.** Add the `PATH` line that `qr install` printed to `~/.zshrc` or `~/.bashrc` yourself (quota-router never edits it), for example:

   ```sh
   export PATH="$HOME/.local/share/quota-router/bin:$PATH"
   ```

   Done when `qr doctor` shows `OK` for the launcher lines, `claude` typed in a new terminal prints one line starting with `[quota-router]` before the session starts (`session on <account>`, or why it used the default account), and `qr decisions` shows a `qr launch` line.

## Kimi Code (fourth family)

Add your logged-in Kimi Code CLI account to the config:

```toml
[[accounts]]
family = "kimi"
key = "kimi-1"
dir = ""

[families.kimi]
default_model = "kimi-code/k3.1-pro-highspeed"
refresh_logins = true
```

Use `kimi -p "summarize the diff"` (or `--prompt`). The hook adds `-m kimi-code/k3.1-pro-highspeed` only when neither `-m` nor `--model` is present. It never adds `--auto` or `-y`: prompt mode uses the CLI's permission settings. `kimi`, `kimi.exe` and `kimi.cmd` are recognised. Interactive sessions and management subcommands are left alone.

`dir = ""` means `~/.kimi-code`. Additional accounts use their own directories, selected with `KIMI_CODE_HOME`; the default directory needs no prefix. `qr init` pre-fills the default and detected `~/.kimi-code-*` directories. Try `qr status`, `qr status --table`, `qr pick kimi --peek`, and `qr check kimi kimi-1 --peek`. Dispatch reservations, running jobs, other-family hints, `qr decisions`, and `qr report` include Kimi.

Kimi's long window is **monthly, not weekly**. `limit_month_total` supplies it; `limit_5h` supplies the five-hour window, and `limit_month_code` is displayed separately. `weekly_floor` and the long-window scoring/hint rules apply to the monthly window. Usage is cached for `usage_cache_seconds` (180 seconds by default). The other families' scoring does not change.

Kimi's short-lived login is renewed by default from `<dir>/credentials/kimi-code.json`. A hook starts a background renewal; ordinary CLI reads renew in place. A failed renewal shows unknown quota and remains eligible for dispatch. Missing credentials block dispatch. Set `[families.kimi] refresh_logins = false` or `QR_NO_REFRESH=1` to disable renewal. Only approved Kimi HTTPS hosts receive these tokens; redirects are refused. New credentials preserve existing fields and are atomically replaced with mode 600. The CLI's OAuth lock is honoured on macOS/Linux. `~/.kimi-code/config.toml` is never changed. See [the reference](docs/REFERENCE.md#kimi-code-family) for endpoint and locking details.

## How it decides

- **Same family only.** You choose the family; the hook chooses the account. The other family's quota is shown as a suggestion.
- **Score.** Weekly % left divided by hours until the weekly reset, so quota about to expire is used first. A five-hour window under 30% scales the score down. Busy accounts are divided by 1 + their jobs (running dispatches or ones sent in the last five minutes). Ties go to the account listed first.
- **Skipped accounts.** Five-hour window under `five_hour_floor`; weekly quota used up; weekly quota under `weekly_floor` (accounts other than the lead's); nobody logged in. An account whose quota cannot be read stays in, with the median score of the readable ones.
- **Lead protection.** The lead's account is left out of automatic picks once its weekly window is below `protect_threshold`, once its five-hour window (after the jobs already running on it) is below `protect_five_hour`, or when its quota cannot be read. With `lead_last_resort = true` it is also picked only when no other account of the family can take the job, so a long batch drains the other accounts first and the conversation keeps its own window.
- **Running jobs count against the five-hour window.** Each job already running on an account (or just dispatched there) is assumed to still take `inflight_five_hour_cost` % of its five-hour window; floors and scores use that projected number. Six dispatches made within the usage cache's few minutes no longer all see the same roomy window.
- **Nothing fits: block.** The whole Bash command is refused, with the reason for each account and the earliest scheduled reset.
- **Money is off by default.** A dispatch that the readings say would draw on purchased Codex credits or Claude paid overage is blocked everywhere: automatic routing, a named account, a resumed conversation, `qr pick` and `qr check`. To allow it for one command, write `QR_ALLOW_CREDITS=1` as a prefix of that one command (`QR_ALLOW_CREDITS=1 codex exec …`; `export` does not count). `general.allow_credits = true` allows it for everything.
- **Resumed conversations stay put.** `claude -p … --resume <id>` and `codex exec resume <id>` go to the account that holds the conversation.
- **Naming an account is honored.** `CLAUDE_CONFIG_DIR=… claude -p …` and `CODEX_HOME=… codex exec …` run as written, with a warning when that account would have been skipped.
- **Fail open.** When quota-router itself fails, or runs past its time budget, the command runs as written.

New sessions started through the launcher follow the same score and skips, with these differences:

- **No lead protection.** There is no lead yet when a session starts. Interactive sessions already running on an account count as jobs there.
- **Only accounts set up for sessions.** An account is a candidate when `launch` is not `false` and its directory's `settings.json` has the quota-router hook. An account whose quota can be read comes before one whose quota cannot.
- **One-shot runs pass through.** `claude -p` started outside Claude Code runs as written unless `[launch] route_print = true`; so do `--help`, `--version`, subcommands, a `CLAUDE_CONFIG_DIR` already set, and commands inside a Claude Code session.
- **Resumed conversations stay put.** `--resume <id>` starts on the account that holds the conversation. `-c` / `--continue` goes to the account of the last session the launcher started in this directory; sessions started without the launcher are unknown to it.
- **It never refuses to start.** When nothing fits, nothing can be read or the config is broken, claude starts on the default account and the line says why (`⚠ PAID OVERAGE` when that account would bill). When quota-router itself cannot run, the launcher starts the first other `claude` on `PATH` unchanged.

The full rules, with every exception: [REFERENCE.md › How it decides](docs/REFERENCE.md#how-it-decides) and [› Sessions and the claude launcher](docs/REFERENCE.md#sessions-and-the-claude-launcher).

## What it reads, sends and changes

- It reads each account's login token and sends it only to that vendor's own endpoints. It sends no telemetry.
- Only with `refresh_logins = true` under `[families.claude]` (off by default): when a Claude account's login has expired (the account sat idle) and no Claude Code process runs on that directory, a background quota-router process renews the login with its refresh token, as Claude Code itself would, and writes it back to the keychain item or `.credentials.json` it came from. Otherwise an idle account could not be read and would never be picked. Kimi renewal is separately enabled by default, as described above. The renewed login normally reaches `security` on stdin; a login stored next to several MCP logins is too long for that and goes on its command line, as Claude Code does it, where other local users can see it while it runs. A login Windows keeps in the Credential Manager is not handled. `refresh_logins = false` under `[families.claude]` or `QR_NO_REFRESH=1` turns this off; details in [REFERENCE.md › Renewing an expired Claude login](docs/REFERENCE.md#renewing-an-expired-claude-login).
- When a live Codex query fails and the local numbers are over two hours old, it starts one small `codex exec` in the background on that account to refresh them. `QR_NO_PROBE=1` turns that off.
- The hook runs before every Bash command and can rewrite it. It inserts an account prefix, adds a configured agy/Kimi model when omitted, or blocks the command. Read `qr decisions` after your first few dispatches.
- Outside its state directory apart from the configured login renewals it writes only the hook entry in each `settings.json` you install into and the `claude` launcher file.
- Anyone who can edit the config file can redirect Claude/Codex tokens (Kimi hosts are restricted), so protect it like `settings.json`.

## Limits that bite in practice

- **Resume with `=`.** `--resume=ID` is treated as a new dispatch and may move to an account without that conversation. Write `--resume ID`.
- **Hidden dispatches.** A dispatch inside `bash -c '…'`, `eval`, a script, or a heredoc fed to a shell is not seen: it is neither routed nor checked for credits. Call `qr pick <family> --env` inside the script.
- **Loops land on one account.** A `for` loop or `xargs` counts as one dispatch, so the whole batch goes to one account. Call `qr pick` inside the loop.
- **Only the Bash tool is hooked.** Agent/Task-tool and workflow subagents run on the lead's account, which the launcher chose when the session started.
- **Nothing is synced between accounts.** Each account directory carries its own `settings.json`, `CLAUDE.md`, MCP servers, model and permissions, so a session or dispatch runs with the setup of the account it lands on.
- **Windows: Git Bash only, least proven.** Commands run through Claude Code's PowerShell tool are not routed, running jobs are counted less reliably, and the `claude.cmd` launcher is untested. The tests run on Windows in CI; day-to-day use so far has been on macOS.

The full table, with the fix for each case: [REFERENCE.md › Known limitations](docs/REFERENCE.md#known-limitations).

## Where to look next

- Accounts, settings and environment variables → [REFERENCE.md › Configuration](docs/REFERENCE.md#configuration)
- What each install step does and why → [REFERENCE.md › Install notes](docs/REFERENCE.md#install-notes)
- The launcher: install record, arguments, `--continue`, fallback → [REFERENCE.md › Sessions and the claude launcher](docs/REFERENCE.md#sessions-and-the-claude-launcher)
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
