# quota-router

[English](README.md)

quota-router 是一個 Claude Code hook，幫你把子代理的工作分到不同的訂閱帳號上。你的 session 準備執行 `claude -p "summarize the diff" --model opus` 時，hook 會先查你列出的每個帳號還剩多少額度，改成在最寬裕的那個帳號上執行 `env CLAUDE_CONFIG_DIR=/home/you/.claude-b claude -p "summarize the diff" --model opus`；沒有帳號接得下、或這件工作會花到付費點數時，它會擋下指令並說明原因。在 session 裡，它只處理透過 Bash 工具執行的 `claude -p`、`codex exec`、`agy -p` 和 `kimi -p`。`qr install` 另外會寫一個 `claude` 啟動器，所以你在終端機打 `claude` 開的 session，會開在剩餘額度最多的 Claude 帳號上。一個 session 就是一個帳號：session 跑起來之後不會被搬走，用內建 Agent／Task 工具或 workflow 開的子代理也跟著它留在那個帳號。

每條被分流的指令，session 都會看到一行像這樣的附註：

```
[quota-router] Automatically routed to bob: weekly 88% left (resets in 4.0 d), 0 jobs running, 1 recently reserved
```

（設定 `language = "zh-TW"` 時改成中文，開頭是 `[帳號分流] 已自動改用`。）

本文用到的三個詞：

- **主 session**（lead）：下指令的那個 Claude Code session。「主 session 的帳號」就是它自己跑在上面的帳號。
- **派工**（dispatch）：hook 在 Bash 指令裡看到的一條 `claude -p …`、`codex exec …`、`agy -p …`、`kimi -p …` 或 `grok -p …`。
- **同一家**（family）：Claude、Codex、agy（Antigravity）、Kimi Code 或 Grok Build。派工只會換到同一家的另一個帳號。

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

3. **把 hook 加進每一個會派工、或想開 session 的 Claude Code 帳號。** 第一行同時會寫入 `claude` 啟動器：

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

6. **讓 session 經過啟動器開。** 把 `qr install` 印出的那行 `PATH` 自己加進 `~/.zshrc` 或 `~/.bashrc`（quota-router 不會改這些檔），例如：

   ```sh
   export PATH="$HOME/.local/share/quota-router/bin:$PATH"
   ```

   `qr doctor` 的啟動器那幾行都是 `OK`、在新的終端機打 `claude` 時 session 開始前先印一行 `[quota-router]` 開頭的說明（`session on <帳號>`，或為什麼用了預設帳號；中文介面是 `[帳號分流]`），而且 `qr decisions` 出現 `qr launch` 那一行，就完成了。

## Kimi Code（第四家族）

把已登入的 Kimi Code CLI 帳號加入設定檔：

```toml
[[accounts]]
family = "kimi"
key = "kimi-1"
dir = ""

[families.kimi]
default_model = "kimi-code/k3.1-pro-highspeed"
refresh_logins = true
```

派工使用 `kimi -p "整理這次修改"`（也支援 `--prompt`）。沒有 `-m` 或 `--model` 時，hook 才補上 `-m kimi-code/k3.1-pro-highspeed`。不會加 `--auto` 或 `-y`；非互動派工使用 Kimi CLI 本身的權限設定。支援 `kimi`、Windows 的 `kimi.exe`／`kimi.cmd`；互動模式與管理子指令不分流。

`dir = ""` 是 `~/.kimi-code`，預設位置不加前綴；其他帳號各用一個目錄，以 `KIMI_CODE_HOME` 選擇。`qr init` 會預填預設位置與偵測到的 `~/.kimi-code-*` 目錄。可以用 `qr status`、`qr status --table`、`qr pick kimi --peek`、`qr check kimi kimi-1 --peek` 檢查；預約、進行中工作、跨家族提示、`qr decisions` 和 `qr report` 都包含 Kimi。

Kimi 的長窗是**月額度，不是週額度**。`limit_month_total` 對應月窗，`limit_5h` 對應五小時窗，`limit_month_code` 另外顯示。設定名稱 `weekly_floor` 保留，但對 Kimi 套在月窗上；長窗評分與提示也使用月窗。用量沿用 `usage_cache_seconds` 快取（預設 180 秒）。其他三家的評分不變。

Kimi 的短效登入預設自動續期，讀寫 `<dir>/credentials/kimi-code.json`。hook 交給背景程序續期，手動查詢則直接完成；續期失敗顯示額度未知，仍可派工，沒有登入憑證則不派。可用 `[families.kimi] refresh_logins = false` 或 `QR_NO_REFRESH=1` 關閉。token 只送 Kimi 自家 HTTPS 主機、不跟隨轉址；保留憑證欄位，以權限 600 的暫存檔原子替換。macOS／Linux 配合 CLI 的 OAuth 鎖。完全不修改 `~/.kimi-code/config.toml`。端點與鎖定細節見[參考文件](docs/REFERENCE.md#kimi-code-family)。

## Grok Build（第五家族）

把已登入的 Grok Build CLI 帳號加入設定檔：

```toml
[[accounts]]
family = "grok"
key = "grok-1"
dir = ""

[families.grok]
default_model = ""        # 例如 "grok-4.7"；空字串表示不補
refresh_logins = false
```

派工使用 `grok -p "整理這次修改"`（也支援 `--single`、`--prompt-file`、`--prompt-json`）。設了 `default_model` 時，沒有 `-m` 或 `--model` 的派工才補上 `-m <模型>`。不會加 `--always-approve`。`grok -r <session id> -p …`、`grok -r -p …`、`grok -c -p …` 會留在存有那段對話的帳號。互動模式的 `grok`（不論有沒有帶開場提示）與 `grok models` 等子指令不分流。

`dir = ""` 是 `~/.grok`，預設位置不加前綴；其他帳號各用一個目錄，以 `GROK_HOME` 選擇。`qr init` 會預填預設位置與偵測到的 `~/.grok-*` 目錄。可以用 `qr status`、`qr pick grok --peek`、`qr check grok grok-1 --peek` 檢查。

Grok 的額度是目前這一期（目前是週）的點數用量，從 CLI 自己用的端點讀取。有隨用隨付額度或預付餘額時視為付費：這期額度用完就擋下，要使用者同意後在那條派工前加 `QR_ALLOW_CREDITS=1` 才放行。access token 效期約六小時，quota-router 不自己續期；登入過期、查詢失敗或沒有登入檔時顯示額度未知，保留上次數字並標「數字是舊的」，照樣可派。設 `refresh_logins = true` 時，登入過期會在背景跑 `grok models`，讓 CLI 在自己的鎖底下續期。細節見[參考文件](docs/REFERENCE.md#grok-build-family)。

## 怎麼挑帳號

- **只在同一家裡挑。** 用哪一家由你決定，hook 只決定用哪個帳號。另一家的額度只當建議附上。
- **分數。** 週剩餘 % 除以距離週重置的小時數，所以快作廢的額度先用。五小時窗低於 30% 時分數往下修。手上有工作的帳號再除以「1＋工作數」（正在跑的派工，或最近五分鐘內派出去的）。同分時用設定檔裡排前面的帳號。
- **會被跳過的帳號。** 五小時窗低於 `five_hour_floor`；週額度用完；主 session 以外的帳號週額度低於 `weekly_floor`；沒有人登入。查不到額度的帳號不會被跳過，會拿到查得到的那些帳號的中位數分數。
- **保護主 session。** 主 session 的帳號週剩低於 `protect_threshold`、五小時窗（扣掉它身上進行中的任務後）低於 `protect_five_hour`、或額度讀不到時，自動挑選就不會挑它。`lead_last_resort = true` 再加一層：只有同一家別的帳號都派不出時才派給主管帳號，所以長批次會先把別的帳號用掉，主管對話自己的窗留著。
- **進行中的任務先扣掉。** 帳號上每個正在跑（或剛派出去）的任務，預估還會吃掉五小時窗的 `inflight_five_hour_cost` %；門檻與分數都用扣完的數字。幾分鐘內連派六批，不會每批都看到同一個寬裕的讀數。
- **整家都接不下：擋下。** 整條 Bash 指令不執行，列出每個帳號的原因和最早的預定重置時間。
- **預設不花錢。** 讀數顯示會扣 Codex 加購點數或產生 Claude 付費超額的派工，一律擋下：自動分流、你寫明的帳號、接續的對話、`qr pick` 和 `qr check` 都一樣。要讓某一條放行，把 `QR_ALLOW_CREDITS=1` 寫成那一條指令的前綴（`QR_ALLOW_CREDITS=1 codex exec …`；`export` 不算）。`general.allow_credits = true` 則是全部放行。
- **接續對話留在原帳號。** `claude -p … --resume <id>` 和 `codex exec resume <id>` 會派到存放那段對話的帳號。
- **你寫明的帳號就照用。** `CLAUDE_CONFIG_DIR=… claude -p …`、`CODEX_HOME=… codex exec …` 照原樣執行；那個帳號本來會被跳過時，附一句警告。
- **出錯就放行。** quota-router 自己出錯、或超過時間預算時，指令照原樣執行。

經過啟動器開的新 session，分數和跳過的規則都一樣，差別在：

- **不保護主 session。** session 剛開始時還沒有主 session。已經在跑的互動 session 會算進那個帳號的工作數。
- **只挑設定好的帳號。** `launch` 不是 `false`、而且那個目錄的 `settings.json` 裝了 quota-router hook，才會是候選。查得到額度的帳號排在查不到的前面。
- **一次性的執行照原樣跑。** 在 Claude Code 外面執行的 `claude -p`，除非設了 `[launch] route_print = true`，否則照原樣執行；`--help`、`--version`、子指令、已經設好的 `CLAUDE_CONFIG_DIR`、在 Claude Code session 裡跑的指令也一樣。
- **接續對話留在原帳號。** `--resume <id>` 開在存放那段對話的帳號。`-c`／`--continue` 會去這個目錄最近一次由啟動器開的 session 所在的帳號；沒經過啟動器開的 session，它不會知道。
- **一定會開得起來。** 沒有帳號合適、什麼都讀不到、或設定檔壞了時，claude 開在預設帳號，並用一行說明原因（那個帳號會扣錢時附 `⚠ PAID OVERAGE`）。quota-router 自己跑不起來時，啟動器會直接啟動 `PATH` 上下一個 `claude`，不做任何改動。

完整規則和所有例外：[REFERENCE.md › How it decides](docs/REFERENCE.md#how-it-decides) 與 [› Sessions and the claude launcher](docs/REFERENCE.md#sessions-and-the-claude-launcher)。

## 它讀什麼、送什麼、改什麼

- 它會讀每個帳號的登入 token，只送到該廠商自己的端點，也不送任何遙測資料。
- 只有在 `[families.claude]` 設 `refresh_logins = true` 時（預設關閉）：Claude 帳號的登入過期了（帳號一直閒置）、而且那個目錄沒有在跑的 Claude Code 程序時，quota-router 會在背景另開一個程序，像 Claude Code 自己那樣用 refresh token 更新登入，再寫回原本讀出來的鑰匙圈項目或 `.credentials.json`。不這樣做的話，閒置帳號讀不到額度，就永遠不會被選到。Kimi 的續期則獨立設定、預設開啟，見上節。更新後的登入平常經由 stdin 交給 `security`；如果登入旁邊還存了好幾個 MCP 登入、長度超過上限，就會跟 Claude Code 一樣改放在命令列上，執行的那一下本機其他使用者看得到。Windows 存在認證管理員（Credential Manager）裡的登入不在處理範圍。在 `[families.claude]` 設 `refresh_logins = false`，或設 `QR_NO_REFRESH=1` 可以關掉；細節見 [REFERENCE.md › Renewing an expired Claude login](docs/REFERENCE.md#renewing-an-expired-claude-login)。
- 只有在 `[families.codex]` 設 `refresh_logins = true` 時（預設關閉）：Codex 登入的 access token 剩不到 `refresh_before_hours`（預設 72）小時就到期、而且那個 `CODEX_HOME` 上沒有可能自己在換新的 Codex 程序時（`codex login` 任何時候都算；其他 Codex 程序，包括桌面版的常駐伺服器，從到期前 30 分鐘起都算），quota-router 會在背景另開一個程序，像 Codex 自己那樣用 refresh token 更新登入，再寫回那個 `auth.json`（整個檔案換新、權限 600、裡面其他內容原樣保留）。Codex 只在被使用時才更新登入，所以閒置大約十天的帳號，往往要等排程失敗才會發現 token 過期、甚至 refresh token 已經失效。細節見 [REFERENCE.md › Renewing a Codex login](docs/REFERENCE.md#renewing-a-codex-login)。
- `[notify] on_refresh_failed`（預設空白）可以指定一條 shell 指令：任何家族的登入更新失敗、或發現 refresh token 已失效時執行，環境變數帶 `QR_FAMILY`、`QR_KEY`、`QR_REASON`、`QR_DETAIL`、`QR_DIR`、`QR_EMAIL`、`QR_EXPIRES_AT`（其餘環境變數只帶 `PATH`、`HOME`、`LANG`、`LC_*`、`TMPDIR`）；同一個帳號、同一個原因，每 `repeat_hours`（預設 12）小時最多一次；暫時性的失敗要連續 `failed_attempts`（預設 2）次才執行。quota-router 自己不送任何通知，指令要做什麼（傳訊息、寄信）由你決定。見 [REFERENCE.md › When a renewal fails](docs/REFERENCE.md#when-a-renewal-fails)。
- 即時查 Codex 失敗、而且本機數字已經超過兩小時，它會在背景用那個帳號跑一次很小的 `codex exec` 來更新數字。設 `QR_NO_PROBE=1` 可以關掉。
- hook 在每條 Bash 指令執行前都會跑，而且可以改寫指令。它會加上帳號前綴、補上省略的 agy／Kimi／Grok 預設模型，或擋下整條指令。前幾次派工之後，請看一下 `qr decisions`。
- 在它自己的狀態目錄之外，除了設定允許的登入續期，它還會寫入：你安裝進去的每個 `settings.json` 裡的 hook 設定，和 `claude` 啟動器檔案。
- 能改設定檔的人就能把 Claude／Codex token 導去別的地方（Kimi、Grok 限自家主機），所以請像保護 `settings.json` 一樣保護它。

## 實際最容易踩到的限制

- **接續對話寫成 `=`。** `--resume=ID` 會被當成新派工，可能被換到沒有那段對話的帳號。請寫 `--resume ID`。
- **看不到的派工。** 寫在 `bash -c '…'`、`eval`、腳本裡，或餵給殼執行的 heredoc 裡的派工，hook 看不到：不分流，也不檢查會不會花點數。請在腳本裡自己呼叫 `qr pick <family> --env`。
- **迴圈只會落在一個帳號。** `for` 迴圈或 `xargs` 算一次派工，整批都會派到同一個帳號。請在迴圈裡呼叫 `qr pick`。
- **只接了 Bash 工具。** 用 Agent／Task 工具或 workflow 開的子代理，都跑在主 session 的帳號上，也就是 session 開始時啟動器挑的那個。
- **帳號之間什麼都不同步。** 每個帳號目錄有自己的 `settings.json`、`CLAUDE.md`、MCP 伺服器、模型和權限設定，session 或派工落在哪個帳號，就用那個帳號的設定。
- **Windows 只支援 Git Bash，驗證最少。** Claude Code 用 PowerShell 工具跑的指令不會分流，正在跑的工作也算得比較不準，`claude.cmd` 啟動器也還沒在真的 Windows 上測過。測試在 CI 的 Windows 上有跑，但日常使用目前都在 macOS。

完整的對照表和每一種的改法：[REFERENCE.md › Known limitations](docs/REFERENCE.md#known-limitations)。

## 接下來看哪裡

詳細文件是英文的：

- 帳號、設定值、環境變數 → [REFERENCE.md › Configuration](docs/REFERENCE.md#configuration)
- 每個安裝步驟在做什麼、為什麼 → [REFERENCE.md › Install notes](docs/REFERENCE.md#install-notes)
- 啟動器：安裝紀錄、參數、`--continue`、退回預設帳號 → [REFERENCE.md › Sessions and the claude launcher](docs/REFERENCE.md#sessions-and-the-claude-launcher)
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
