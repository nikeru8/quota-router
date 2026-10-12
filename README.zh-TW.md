# quota-router

[English](README.md)

quota-router 是一個 Claude Code hook，幫你把子代理的工作分到不同的訂閱帳號上。你的 session 準備執行 `claude -p "summarize the diff" --model opus` 時，hook 會先查你列出的每個帳號還剩多少額度，改成在最寬裕的那個帳號上執行 `env CLAUDE_CONFIG_DIR=/home/you/.claude-b claude -p "summarize the diff" --model opus`；沒有帳號接得下、或這件工作會花到付費點數時，它會擋下指令並說明原因。它只處理 Claude Code session 透過 Bash 工具執行的 `claude -p` 和 `codex exec`。session 本身始終待在它自己那一個帳號上，用內建 Agent／Task 工具或 workflow 開的子代理也跟著它留在原帳號。

每條被分流的指令，session 都會看到一行像這樣的附註：

```
[quota-router] Automatically routed to bob: weekly 88% left (resets in 4.0 d), 0 jobs running, 1 recently reserved
```

（設定 `language = "zh-TW"` 時改成中文，開頭是 `[帳號分流] 已自動改用`。）

本文用到的三個詞：

- **主 session**（lead）：下指令的那個 Claude Code session。「主 session 的帳號」就是它自己跑在上面的帳號。
- **派工**（dispatch）：hook 在 Bash 指令裡看到的一條 `claude -p …` 或 `codex exec …`。
- **同一家**（family）：Claude 或 Codex。派工只會換到同一家的另一個帳號。

## 需求

- Python 3.11 以上，不需要第三方套件。
- 主 session 用 Claude Code，而且版本要認得 exec 形式的 hook（`command` 加 `args`）。安裝第 5 步就是在確認這件事。
- 同一家至少兩個已登入的帳號，各放在自己的目錄：執行 `CLAUDE_CONFIG_DIR=~/.claude-b claude`（或 `CODEX_HOME=~/.codex-b codex`），在裡面登入一次。
- 選用：Codex CLI，如果你也會派 `codex exec`。
- macOS、Linux，或裝了 [Git for Windows](https://git-scm.com/download/win) 的 Windows（Claude Code 的 Bash 工具在那裡是跑在 Git Bash 裡）。

## 安裝

先從 GitHub 安裝套件（還沒上 PyPI）：

```sh
pip install git+https://github.com/nikeru8/quota-router
```

`qr --help` 印出指令清單就表示裝好了。
Homebrew、Debian 和多數發行版的 Python 會回 "externally-managed-environment"，或者根本沒有 `pip`：改用 `python3 -m venv ~/.qr-venv && ~/.qr-venv/bin/pip install git+https://github.com/nikeru8/quota-router`，之後用 `~/.qr-venv/bin/qr`，或把 `~/.qr-venv/bin` 加進 `PATH`。

1. **建立設定檔。**

   ```sh
   qr init
   ```

   印出新設定檔的路徑和預填的帳號，就完成了。

2. **把設定檔裡的 `[[accounts]]` 改成你實際登入的帳號**，再看工具讀到什麼：

   ```sh
   qr config
   qr status
   ```

   設定檔裡每個帳號在 `qr status` 都有額度讀數，就完成了。只留下有讀數的帳號。

3. **把 hook 加進每一個會派工的 Claude Code 帳號：**

   ```sh
   qr install                              # ~/.claude/settings.json
   qr install --config-dir ~/.claude-b     # 其他 Claude 帳號各跑一次
   ```

   結束碼是 0 就完成了；原本的檔案會留一份 `settings.json.bak-<時間>`。

4. **檢查安裝。**

   ```sh
   qr doctor
   ```

   每一行都是 `OK` 開頭就完成了。就算印出 `WARN`，結束碼也還是 0，所以請逐行看。

5. **試一次。** 開一個新的 Claude Code session，請它執行：

   ```sh
   claude -p "say hi" --model sonnet
   ```

   工具回覆裡有一行以 `[quota-router] Automatically routed to`（中文介面是 `[帳號分流] 已自動改用`）開頭，就成功了。`qr decisions` 可以看每次怎麼決定、為什麼。

## 怎麼挑帳號

- **只在同一家裡挑。** 用 Claude 還是 Codex 由你決定，hook 只決定用哪個帳號。另一家的額度只當建議附上。
- **分數。** 週剩餘 % 除以距離週重置的小時數，所以快作廢的額度先用。五小時窗低於 30% 時分數往下修。手上有工作的帳號再除以「1＋工作數」（正在跑的派工，或最近五分鐘內派出去的）。同分時用設定檔裡排前面的帳號。
- **會被跳過的帳號。** 五小時窗低於 `five_hour_floor`；週額度用完；主 session 以外的帳號週額度低於 `weekly_floor`；沒有人登入。查不到額度的帳號不會被跳過，會拿到查得到的那些帳號的中位數分數。
- **保護主 session。** 主 session 的帳號最緊的那個窗低於 `protect_threshold`、或額度讀不到時，自動挑選就不會挑它。
- **整家都接不下：擋下。** 整條 Bash 指令不執行，列出每個帳號的原因和最早的預定重置時間。
- **預設不花錢。** 讀數顯示會扣 Codex 加購點數或產生 Claude 付費超額的派工，一律擋下：自動分流、你寫明的帳號、接續的對話、`qr pick` 和 `qr check` 都一樣。要讓某一條放行，把 `QR_ALLOW_CREDITS=1` 寫成那一條指令的前綴（`QR_ALLOW_CREDITS=1 codex exec …`；`export` 不算）。`general.allow_credits = true` 則是全部放行。
- **接續對話留在原帳號。** `claude -p … --resume <id>` 和 `codex exec resume <id>` 會派到存放那段對話的帳號。
- **你寫明的帳號就照用。** `CLAUDE_CONFIG_DIR=… claude -p …`、`CODEX_HOME=… codex exec …` 照原樣執行；那個帳號本來會被跳過時，附一句警告。
- **出錯就放行。** quota-router 自己出錯、或超過時間預算時，指令照原樣執行。

完整規則和所有例外：[REFERENCE.md › How it decides](docs/REFERENCE.md#how-it-decides)。

## 它讀什麼、送什麼、改什麼

- 它會讀每個帳號的登入 token（唯讀），只送到該廠商自己的用量端點。它不會更新、改寫或複製 token，也不送任何遙測資料。
- 即時查 Codex 失敗、而且本機數字已經超過兩小時，它會在背景用那個帳號跑一次很小的 `codex exec` 來更新數字。設 `QR_NO_PROBE=1` 可以關掉。
- hook 在每條 Bash 指令執行前都會跑，而且可以改寫指令。它只會加上帳號前綴，或擋下整條指令。前幾次派工之後，請看一下 `qr decisions`。
- 能改設定檔的人就能把你的 token 導去別的地方，所以請像保護 `settings.json` 一樣保護它。

## 實際最容易踩到的限制

- **接續對話寫成 `=`。** `--resume=ID` 會被當成新派工，可能被換到沒有那段對話的帳號。請寫 `--resume ID`。
- **看不到的派工。** 寫在 `bash -c '…'`、`eval`、腳本裡，或餵給殼執行的 heredoc 裡的派工，hook 看不到：不分流，也不檢查會不會花點數。請在腳本裡自己呼叫 `qr pick <family> --env`。
- **迴圈只會落在一個帳號。** `for` 迴圈或 `xargs` 算一次派工，整批都會派到同一個帳號。請在迴圈裡呼叫 `qr pick`。
- **只接了 Bash 工具。** 用 Agent／Task 工具或 workflow 開的子代理，都跑在主 session 的帳號上。
- **Windows 只支援 Git Bash，驗證最少。** Claude Code 用 PowerShell 工具跑的指令不會分流，正在跑的工作也算得比較不準。測試在 CI 的 Windows 上有跑，但日常使用目前都在 macOS。

完整的對照表和每一種的改法：[REFERENCE.md › Known limitations](docs/REFERENCE.md#known-limitations)。

## 接下來看哪裡

詳細文件是英文的：

- 帳號、設定值、環境變數 → [REFERENCE.md › Configuration](docs/REFERENCE.md#configuration)
- 每個安裝步驟在做什麼、為什麼 → [REFERENCE.md › Install notes](docs/REFERENCE.md#install-notes)
- 所有指令，以及在腳本裡用 `qr pick` → [REFERENCE.md › Commands](docs/REFERENCE.md#commands)（也可以看 `qr --help`）
- 每一種訊息和它的意思 → [REFERENCE.md › Messages you will see](docs/REFERENCE.md#messages-you-will-see)
- 隱私：讀了什麼、送了什麼、記了什麼 → [REFERENCE.md › Privacy and security](docs/REFERENCE.md#privacy-and-security)
- macOS、Linux、Windows 的差異 → [REFERENCE.md › Platform notes](docs/REFERENCE.md#platform-notes)
- 移除 → [REFERENCE.md › Uninstall](docs/REFERENCE.md#uninstall)
- 為什麼這樣設計 → [REFERENCE.md › FAQ](docs/REFERENCE.md#faq)
- 測試、從原始碼直接跑 → [REFERENCE.md › Development](docs/REFERENCE.md#development)
- 每個版本的內容 → [CHANGELOG.md](CHANGELOG.md)

## 免責聲明

quota-router 用的是 Anthropic 和 OpenAI **沒有公開文件的用量端點**；它們隨時可能改掉或消失，到時工具會讀不到額度，直到更新為止。
本工具**與 Anthropic、OpenAI 沒有任何關係，也沒有得到它們的認可或支援**。同時使用多個訂閱帳號是否符合各家的使用條款，請你自己確認。
**使用風險自負。** 本工具依現狀提供，不附任何保證。

## 授權

MIT，見 [LICENSE](LICENSE)。
