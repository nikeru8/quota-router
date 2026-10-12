# quota-router

[English](README.md)

quota-router 是一個 Claude Code 的 `PreToolUse` hook，給「用不只一個訂閱帳號跑子代理」的人用。你的 Claude Code session 每次要執行 `claude -p …` 或 `codex exec …` 之前，hook 會先查你列出的每個帳號的額度（即時查詢，結果在幾分鐘內重複使用），在同一個家族（Claude 或 Codex）裡挑最寬裕的那個，把它的環境變數加到指令前面。沒有帳號接得下這件工作、或這件工作會花到付費點數時，指令會被擋下並說明原因。

session 輸入的是：

```sh
claude -p "summarize the diff" --model opus
```

hook 把它改成：

```sh
env CLAUDE_CONFIG_DIR=/home/you/.claude-b claude -p "summarize the diff" --model opus
```

並附一行說明給 session 看：

```
[帳號分流] 已自動改用 bob：週剩 88%（4.0 天後重置），跑 0、剛派 1
```

沒有帳號可派時，指令不會執行，session 會收到原因。下面這個例子是兩個 Codex 帳號的訂閱額度都用完了，其中一個有加購點數：

```
第 1 個派工（codex／other）被擋，整條指令未執行。
沒有可派帳號
codex-1：週額度用完
codex-2：訂閱額度用完，會改扣加購點數；點數餘額 40
最早預定重置：codex-1 共用週額度 10-15 21:34（預定，不保證屆時可派）
使用者同意後，把 QR_ALLOW_CREDITS=1 寫成那條派工的前綴（例如 QR_ALLOW_CREDITS=1 codex exec …；export 不算）
另一家 Claude：alice 15%（預定 4.0 天後重置） 跑 0／bob 0%（預定 4.0 天後重置） 跑 0（數字即時）
要硬派請明寫帳號（會扣點數的情況不適用）；仍有待辦時稍後再派一次，工具會重查
這個開關只有使用者當次同意才可以加，請先回報使用者，不要自行加上
```

`alice`、`bob` 和帳號 key 都是虛構的。訊息裡的帳號用登入信箱 `@` 前面那一段來稱呼；信箱還不知道、或是在列帳號清單時，用設定檔裡的 key。上面是 `language = "zh-TW"` 的樣子，預設語言是英文（`[quota-router] Automatically routed to …`）。

session 本身，以及它在自己程序裡做的事（內建的 Agent 工具、workflow），都留在它啟動時的帳號。為此 `qr install` 也會寫一個 `claude` 啟動器：把它放到 `PATH` 最前面，在終端機打 `claude` 就會開在額度最多的 Claude 帳號。見[開 session 也會挑帳號](#開-session-也會挑帳號)。

## 為什麼需要它

- **好幾個帳號，一個 session。** 子代理跑在哪個帳號，看它的環境變數指到哪裡。不管它的話，全部都會擠在主管 session 自己用的那個帳號上。
- **週額度不用就作廢。** 每個帳號各有自己的重置時間，沒用完的不會留到下一週。應該先用最快要重置的那個。
- **主管 session 不會自己去看額度。** 沒有東西告訴它，它就看不到；也不會每次派工前先查一次。
- **額度用完可能默默開始花錢。** 有加購點數或開了付費超額的帳號，訂閱額度用完後照樣能跑，只是改成扣錢。

## 需求

- Python 3.11 以上，不需要第三方套件。
- 主管 session 是 [Claude Code](https://claude.com/claude-code)。這是 Claude Code 的 hook，單獨執行沒有作用。`qr install` 寫的是 exec 形式的 hook（`command` 加 `args`），所以你的 Claude Code 版本要認得這個形式。最低版本沒有驗證過；安裝的第 5 步就是用來確認的。
- 選用：Codex CLI（你也會派 `codex exec` 的話）。
- macOS、Linux 或 Windows。Windows 必須裝 [Git for Windows](https://git-scm.com/download/win)：Claude Code 的 Bash 工具跑在 Git Bash 裡，而這個 hook 只看得到 Bash 指令。
- 同一個家族至少兩個登入好的帳號、各放在自己的目錄，否則沒有東西可以分流。新增帳號的做法：`CLAUDE_CONFIG_DIR=~/.claude-b claude`（或 `CODEX_HOME=~/.codex-b codex`），在裡面登入一次。每個目錄是一份獨立的 CLI 設定：模型、權限、指示檔、外掛整合都以那個目錄裡的為準。quota-router 不會幫你同步，所以被分流的工作是用它落腳的那個帳號的設定在跑。

## 安裝

還沒上 PyPI，先從 GitHub 裝：

```sh
pip install git+https://github.com/nikeru8/quota-router
```

Python 不准直接裝套件時（macOS 的 Homebrew、Debian 與多數發行版的 Python 都是，錯誤訊息寫 "externally-managed-environment"），或是根本沒有 `pip` 這個指令時，改裝進虛擬環境：

```sh
python3 -m venv ~/.qr-venv
~/.qr-venv/bin/pip install git+https://github.com/nikeru8/quota-router
~/.qr-venv/bin/qr --help
```

之後用 `~/.qr-venv/bin/qr` 這個路徑呼叫，或把 `~/.qr-venv/bin` 加進 `PATH`（有 pipx 的話 `pipx install git+https://github.com/nikeru8/quota-router` 一步做完）。`qr` 不必在 Claude Code 的 `PATH` 上：hook 安裝時寫的是 Python 的絕對路徑。

1. **建立設定檔。**

   ```sh
   qr init        # 寫出設定檔，並列出找到的帳號
   ```

   它會預填兩個 CLI 的預設位置，以及每個看起來像帳號的 `~/.claude-*`、`~/.codex-*` 目錄。判斷方式只看某幾個檔案存不存在，不讀任何檔案的內容。**列出來不代表已經登入**：兩個預設位置一定會列，就算這台機器從來沒用過那個 CLI。

2. **編輯設定檔。** 打開 `qr init` 印出的路徑，把 `[[accounts]]` 改成和你實際的登入一致（見[設定](#設定)）。然後看工具讀到什麼，並把 `qr status` 顯示沒有讀數、其實沒有人登入的帳號從設定檔刪掉：

   ```sh
   qr config      # 每個設定的值與來源
   qr status      # 每個帳號一列，顯示目前額度
   ```

3. **把 hook 加進每一個會派工的 Claude Code 帳號。** 每個帳號目錄各有自己的 `settings.json`：

   ```sh
   qr install                              # ~/.claude/settings.json
   qr install --config-dir ~/.claude-b     # 其他 Claude 帳號各跑一次
   ```

   它只加一條 `Bash` 的 `PreToolUse` 設定，檔案其餘內容維持原樣；原檔另存成同目錄的 `settings.json.bak-<時間>`。重複執行不會有任何變化。寫完後它會拿一個無害的測試事件把裝好的指令實際跑一次，那條指令沒有回應時結束碼是 1。安裝成功只代表 hook 啟動得了，不代表分流已經設定好：沒有設定檔時 hook 一樣會回應這個測試。

   沒有 `--config-dir` 時，它也會把 `claude` 啟動器寫到 `~/.local/share/quota-router/bin/claude`（見[開 session 也會挑帳號](#開-session-也會挑帳號)）；那個目錄還沒排在真正的 `claude` 前面時，會印出要加進 `~/.zshrc` 或 `~/.bashrc` 的那一行。它自己絕不改這些檔案。`--no-launcher` 只裝 hook；`--config-dir` 永遠不動啟動器。新 session 只會開在有 hook 的帳號目錄，所以每個想開 session 的帳號都要用 `--config-dir` 裝一次。

4. **檢查安裝。**

   ```sh
   qr doctor      # 每項檢查一行 OK 或 WARN
   ```

   請看每一行：`qr doctor` 就算印了警告，結束碼仍是 0。

5. **試一次。** 開一個新的 Claude Code session，請它執行

   ```sh
   claude -p "say hi" --model sonnet
   ```

   工具回覆裡應該有一行以 `[帳號分流] 已自動改用`（英文介面是 `[quota-router] Automatically routed to`）開頭。`qr decisions` 可以看每次的決定與理由。

   啟動器的目錄放上 `PATH` 之後，開一個新的終端機執行 `claude`：session 開始前會先印一行 `[帳號分流] 這個 session 開在`（英文介面是 `[quota-router] session on`）。

不安裝、直接從原始碼跑：`git clone` 之後用 `bin/qr <指令>`（Windows 是 `python bin\qr <指令>`）。

## 設定

設定檔位置：有設 `$QR_CONFIG` 就用它，否則是 `~/.config/quota-router/config.toml`（Windows：`%APPDATA%\quota-router\config.toml`）。只有 `[[accounts]]` 是必填。

```toml
[general]
language = "zh-TW"

[[accounts]]
family = "claude"
key = "claude-main"
dir = ""                   # 空字串＝CLI 的預設位置

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

### 帳號

| 欄位 | 預設 | 意思 |
|---|---|---|
| `family` | 必填 | `claude` 或 `codex`。 |
| `key` | 必填 | 自己取的、不重複的名稱（英數字、`-`、`_`、`.`），會出現在訊息與紀錄裡。 |
| `dir` | `""` | 你平常寫在 `CLAUDE_CONFIG_DIR`／`CODEX_HOME` 的目錄，`~` 與 `$HOME` 會展開。空字串、或等於 `~/.claude`／`~/.codex`，表示 CLI 的預設位置；每個家族只能有一個帳號用預設位置。 |
| `launch` | `true` | 只用於 Claude 帳號。`false`：[`qr launch`](#開-session-也會挑帳號) 永遠不會在這裡開新 session。就算是 `true`，也要等這個目錄的 `settings.json` 有 hook 才會在這裡開 session。不論哪種，hook 的派工照樣會派過來，存在這裡的對話也照樣在這裡接續。 |

帳號的排列順序在三種情況有影響：兩個帳號分數完全相同時，用排在前面的那個；兩個 Claude 目錄其實是同一個帳號時，由第一個不是主管所在的目錄代表它；時間預算不夠查完所有帳號時，讀數一樣舊的帳號照這個順序查。

兩個 Claude 目錄登入同一個帳號（信箱相同）時視為一個帳號：額度只算一份，忙碌數相加。

一個帳號都沒列的家族完全不管：它的派工不分流、不檢查，所以也沒有花錢保護。

### 設定值

| 設定 | 預設 | 意思 |
|---|---|---|
| `general.language` | `"en"` | 訊息語言，`en` 或 `zh-TW`。`QR_LANG` 優先。 |
| `general.protect_threshold` | `20` | 主管自己的帳號最緊的額度窗（週或五小時）低於這個 % 就不會被自動挑去接子代理。明寫這個帳號、或接續存放在這個帳號的對話，仍然照用。 |
| `general.allow_credits` | `false` | `true` 表示所有派工都可以花加購點數／付費超額，不必加 `QR_ALLOW_CREDITS=1` 前綴。 |
| `hints.home_low` | `30` | 本家族最佳週剩低於這個 % 就開始看另一家族…… |
| `hints.other_high` | `50` | ……另一家族最佳週剩至少這個 %、或至少兩倍時，提示改派。 |
| `hints.other_idle` | `90` | 另一家族有閒置帳號週剩至少這個 %…… |
| `hints.home_busy` | `3` | ……而本家選到的帳號已有這麼多個任務、或週剩不到對方一半時，也提示。 |
| `limits.five_hour_floor` | `10` | 五小時窗低於這個 % 的帳號不派。 |
| `limits.weekly_floor` | `10` | 非主管帳號週剩低於這個 % 不派。 |
| `limits.tight_warning` | `15` | 最佳帳號週剩低於這個 % 就警告全部都緊。 |
| `timing.usage_cache_seconds` | `180` | 用量讀數在這段時間內算新鮮。 |
| `timing.profile_cache_seconds` | `86400` | 帳號信箱的暫存時間。 |
| `timing.lease_seconds` | `1800` | 派工留下的預約多久後自動解除。 |
| `timing.busy_window_seconds` | `300` | 預約在這段時間內算「剛派」（計入忙碌）。 |
| `timing.hook_budget_seconds` | `8` | hook 處理一條指令可以花的時間。超過兩秒後 hook 程序會被結束，指令照原樣執行。 |
| `timing.codex_stale_hours` | `2` | Codex 非即時數字超過這個時數視為未知。 |
| `timing.codex_probe_hours` | `2` | 同一個帳號兩次背景探測至少間隔這個時數。 |
| `timing.notice_ttl_hours` | `6` | 重置通知的有效時數。 |
| `events.reset_drop_points` | `3` | 已用下降這麼多點才算重置或補額度。 |
| `families.claude.scarce_model` | `"fable"` | 有專屬週額度的 Claude 模型。`""` 表示關掉分模型的檢查。 |
| `launch.route_print` | `false` | `true`：在 Claude Code 之外啟動的 `claude -p …` 也由[啟動器](#開-session-也會挑帳號)挑帳號。預設照原樣執行。 |
| `endpoints.claude_usage` | `"https://api.anthropic.com/api/oauth/usage"` | 查 Claude 用量的網址。 |
| `endpoints.claude_profile` | `"https://api.anthropic.com/api/oauth/profile"` | 查 Claude 帳號信箱的網址。 |
| `endpoints.codex_usage` | `"https://chatgpt.com/backend-api/wham/usage"` | 查 Codex 用量的網址。 |

`[endpoints]` 只給測試、以及要經過代理才連得到原廠的網路環境用。**你的登入 token 會送到這三個網址**，其他情況請不要改。`qr init` 寫出來時這一段是註解掉的。

不認得的段落、設定名稱與帳號欄位只警告、不報錯；警告由 `qr config` 與 `qr doctor` 印出，hook 與其他指令不會重複。型別不對、語言不認得、key 重複、沒有任何帳號則是錯誤：需要設定檔的指令（`status`、`pick`、`check`、`report`、`config` 等）結束碼 2；`qr doctor` 把它列成一行 `WARN`、結束碼 0；hook 讓指令照原樣執行，只在 stderr 留一行。完整的檢查規則在 [docs/REFERENCE.md](docs/REFERENCE.md)（英文）。

### 環境變數

| 變數 | 作用 |
|---|---|
| `QR_CONFIG` | 設定檔路徑。 |
| `QR_STATE` | 狀態目錄。預設 `~/.local/state/quota-router`（或在 `$XDG_STATE_HOME` 底下）；Windows 是 `%LOCALAPPDATA%\quota-router`。 |
| `QR_LANG` | `en` 或 `zh-TW`，優先於設定檔。 |
| `QR_ALLOW_CREDITS=1` | 寫成某一條派工的前綴（`QR_ALLOW_CREDITS=1 codex exec …`）時，允許那一條花點數。對 hook 來說 `export` 不算；`qr pick`／`qr check` 則是讀環境變數。 |
| `QR_NO_PROBE=1` | 不啟動背景的 Codex 探測（見[隱私與安全](#隱私與安全)）。 |
| `QR_DEBUG=1` | 另外把完整的錯誤堆疊寫進狀態目錄的 `debug.log`。這個檔可能含敏感資料。 |

### 指令

```
qr init [--force]                    寫出範例設定檔
qr config                            印出解析後的設定與來源
qr install [--config-dir 目錄] [--no-launcher]
                                     把 hook 加進 settings.json（預設 ~/.claude），並寫入 claude 啟動器
qr uninstall [--config-dir 目錄] [--no-launcher]
                                     移除 `qr install` 加的 hook 與啟動器
qr launch [--] [claude 參數]         在額度最多的 Claude 帳號上啟動 claude（啟動器跑的就是這個）
qr doctor                            逐項檢查安裝狀況，每項一行 OK／WARN
qr status                            所有帳號的表格
qr pick <家族>                       印出最佳帳號的環境變數前綴並預約
                                     （--env、--peek、--model <名稱>；--strict 可以寫但沒有作用）
qr check <家族> <key|第幾個|目錄>    檢查指定帳號
qr report [--days N]                 重置事件、目前步調、派工統計
qr decisions [N]                     最近 N 筆分流決定
qr leases --clear                    清掉所有預約
```

hook 只讀 Bash 指令的文字。指令所執行的腳本裡面才發生的派工不在那段文字裡，所以腳本要自己問帳號。結束碼 0 會印出前綴；結束碼 3 時 stdout 什麼都不印，原因在 stderr。除非環境變數裡有 `QR_ALLOW_CREDITS=1`，`qr pick` 不會回傳會花點數的帳號：

```sh
account=$(qr pick codex --env) || exit 3
eval "env $account codex exec \"\$prompt\""
```

要經過 `eval` 是因為目錄裡有空格或其他特殊字元時，前綴會帶引號。

## 怎麼決定

- **只在同一家族裡挑。** 用 Claude 還是 Codex 由你決定；hook 只挑帳號，不會換家族。
- **讀數。** 每個帳號的額度是即時查的，結果在 `usage_cache_seconds` 內重複使用。端點剛失敗而在冷卻中、另一個 hook 程序正在查同一個帳號、或時間預算快用完時不會再查，改用最後一次知道的數字。
- **分數。** 週剩餘 % 除以距離週重置的小時數，所以快作廢的額度先用。五小時窗低於 30% 時分數等比例下修。結果再除以「1＋該帳號的任務數」；任務數取「正在跑的 `claude -p`／`codex exec` 程序數」與「最近五分鐘內派出去的數量」兩者較大的。分數最高的勝出；同分時用設定檔裡排在前面的帳號。已經允許花錢時，不會花錢的帳號一律排在會花錢的前面，不看分數。
- **會被跳過的帳號。** 五小時窗低於 `five_hour_floor`；週額度用完；主管以外的帳號週剩低於 `weekly_floor`；帳號目錄裡找不到登入資料。
- **查不到額度的帳號不會被跳過。** 它拿到的是「查得到的那些帳號」的中間分數，附註會寫額度未知。所以你列了、但暫時查不到的帳號（端點掛了、token 過期）還是可能接到工作。
- **數字舊的照派，不會因此不派。** 所有帳號都被擋時，不新鮮的讀數會先重查一次。重查後數字仍然是舊的、不是主管的帳號、而且不可能花到錢的，會照派並附一行 `⚠` 說明數字是舊的。
- **保護主管。** 你的 session 自己所在的帳號低於 `protect_threshold`、或額度讀不到時，不會被挑中。這只管自動挑選：你自己寫明那個帳號、或接續存放在那裡的對話，仍然照用。
- **整家沒有帳號可派：擋下。** 整條 Bash 指令不執行，並列出每個帳號的原因、最早的預定重置時間，以及（查得到的話）另一家族的狀況。
- **預設不花錢。** 讀數顯示會扣 Codex 加購點數或產生 Claude 付費超額的派工一律擋下：自動分流、你自己寫了帳號、接續對話、`qr pick`／`qr check` 都一樣。放行的方式只有兩個：把 `QR_ALLOW_CREDITS=1` 寫成那條指令的前綴，或在設定檔寫 `general.allow_credits = true` 對所有派工放行。這層檢查的界線：它靠的是讀數，所以付費狀態不明時（帳號目錄不在設定檔裡、接續的對話找不到存在哪個帳號、讀數裡沒有點數資訊）會放行並附註；已經在跑的工作把額度用完之後繼續用付費額度，它也攔不到。
- **接續對話留在原帳號。** `claude -p … --resume <id>` 與 `codex exec resume <id>` 會派到存放那段對話的帳號。那個帳號額度用完時指令被擋，並請你附上前一輪摘要、換帳號重派。
- **你寫了帳號就照用。** `CLAUDE_CONFIG_DIR=… claude -p …`、`CODEX_HOME=… codex exec …` 照原樣執行；那個帳號本來會被跳過時附一句警告。變數在派工之前又被清掉（`unset`、`env -i`、`env -u CODEX_HOME`）就等於沒寫帳號，照一般方式分流。唯一的例外：`env -u CLAUDE_CONFIG_DIR claude -p …` 是「指定預設位置那個 Claude 帳號」的寫法，會照這個意思處理。
- **另一家族只是建議。** 另一家族有設定帳號、而且來得及查到時，派工會附一行它的額度摘要，明顯比較寬裕時多一行 💡。一條指令裡有好幾個派工時，摘要以第一個為準。要不要改派由主管決定。
- **某個模型可以有自己的額度。** 設了 `scarce_model` 時，寫了那個模型、或完全沒寫模型的 Claude 派工，還會多檢查那個模型的專屬週額度。沒寫 `--model` 的接續對話，只要共用額度還有就留在原帳號，但會不會產生付費超額是照那份專屬額度判斷的。用量端點沒有回報那份額度時，就沒有東西可以檢查。
- **重置看讀數，不靠推測。** 即時讀數顯示已用下降至少 `reset_drop_points` 點時，記成一次重置或補額度，每個 session 通知主管一次。五小時窗照預定時間換窗不通知；只是重置日期變了的，記在 `qr report` 裡、不另外通知。
- **出錯就放行。** quota-router 自己壞掉時，指令照原樣執行。hook 以 `hook_budget_seconds` 秒內回答為目標；再過兩秒還沒結束，看門狗會直接結束它、不做任何決定。Python 啟動的時間另計。

## 開 session 也會挑帳號

hook 只看得到 session 透過 Bash 工具執行的 `claude -p`／`codex exec`。session 在自己程序裡做的事（內建 Agent 工具、workflow）都跑在它啟動時的帳號。啟動器就是在 session 開始時挑這個帳號。

`qr install` 會在 `~/.local/share/quota-router/bin/` 寫一個叫 `claude` 的小腳本（有設 `$XDG_DATA_HOME` 時是 `$XDG_DATA_HOME/quota-router/bin/`；Windows 是 `%LOCALAPPDATA%\quota-router\bin\claude.cmd`）。在 `~/.zshrc` 或 `~/.bashrc` 把這個目錄放到 `PATH` 最前面：

```sh
export PATH="$HOME/.local/share/quota-router/bin:$PATH"
```

之後在終端機打 `claude`，啟動器會先問 `qr launch` 要用哪個帳號，再帶著那個帳號的 `CLAUDE_CONFIG_DIR` 啟動真正的 `claude`（預設位置的帳號則是不設這個變數）。真正的 `claude` 每次啟動都會越過啟動器的目錄、在 `PATH` 上重新找，所以更新或搬動它都不用做任何事。stderr 是終端機時，會有一行說明開在哪裡：

```
[帳號分流] 這個 session 開在 bob：週剩 88%（4.0 天後重置）
```

**哪些帳號可以開 session。** 只有「設定好」的帳號會拿來開新 session：`launch` 不是 `false`，而且 `qr install` 已經把 hook 寫進那個目錄的 `settings.json`（`qr install --config-dir ~/.claude-b`）。每個帳號目錄各有自己的設定：hook、權限、MCP 伺服器、模型、輸出風格和 `CLAUDE.md`。quota-router 不會在目錄之間複製這些，所以 session 只會開在你設定好的地方。因為缺 hook 而被略過的帳號，`qr status`、`qr config`、`qr doctor` 都會列出來。

怎麼挑：

- **新 session** 照 hook 的規則，在設定好的帳號裡挑最好的：同樣的分數（週剩餘 % 除以距離重置的小時數，五小時窗緊或有任務在跑時下修）、同樣的跳過條件（五小時下限、週下限、週額度用完、沒有登入、未允許時會付費超額的帳號），也會像 `qr pick` 一樣預約。和派工有三點不同：這時還沒有主管，所以沒有帳號會被當成主管的帳號保留；帳號上已經在跑的互動 session 算成那個帳號的任務；讀得到額度的帳號排在讀不到的前面。沒寫 `--model` 時，跟派工一樣也會檢查稀缺模型的額度。
- **接續對話**（`--resume <id>`、`-r <id>`、`--resume=<id>`）開在存放那段對話的帳號。`--continue`／`-c`、不帶 id 的 `--resume`（選單）和 `--from-pr`，開在這個目錄最近一次透過啟動器開 session 的帳號；沒有這種紀錄時，開在這個目錄最新、而且不是 `claude -p` 留下的對話所在的帳號。就算那個帳號沒設定好或額度用完也一樣，只是那一行會帶 `⚠`。找不到帳號時開在預設帳號。
- **不動、也不印任何東西：** 已經設了 `CLAUDE_CONFIG_DIR`（你或 hook 選過了）；在 Claude Code session 裡執行的指令（有 `CLAUDECODE`：那些由 hook 處理）；`claude -p …`（腳本和 `qr pick` 用 `env -u CLAUDE_CONFIG_DIR claude -p …` 表示預設帳號；要讓 Claude Code 以外啟動的單次執行也分流，設 `[launch] route_print = true`）；`--help`、`-h`、`--version`、`-v`、`--bg`；子指令：`mcp`、`config`、`doctor`、`update`、`install`、`auth`、`plugin`、`setup-token`、`migrate-installer`，以及其他出現在第一個位置的單一小寫英文字。所以 `claude hello` 不會分流，`claude "hello there"` 會。
- **絕不拒絕。** 所有帳號都被擋、讀不到任何帳號的額度、設定檔不存在或有問題、quota-router 自己出錯、或挑帳號超過 `hook_budget_seconds` 再加兩秒時，session 開在預設帳號，那一行會說明原因。會付費超額的帳號不會被自動選到；session 還是落在這種帳號時（接續對話、退回預設帳號、或有 `QR_ALLOW_CREDITS=1`／`allow_credits = true`），那一行會寫 `⚠ 付費超額`。
- **絕不擋路。** quota-router 本身跑不起來時（安裝它的 Python 不見了、套件被移除或是沒有 `qr launch` 的舊版、或它自己出錯），啟動器會照原樣啟動 `PATH` 上下一個 `claude`。`qr doctor` 會透過啟動器跑一次 `claude --version`，確認它真的能啟動 claude。

它不做的事：

- **它不會搬動執行中的 session。一個 session 只用一個帳號。** Agent 工具起的子代理與 workflow 一直留在 session 的帳號，額度再低也一樣；要換帳號請開新的 session。
- 它不改 `~/.zshrc` 或任何啟動檔。`qr doctor` 會指出擋路的 `claude` alias 或函式。本身就是執行 `claude` 的 alias（`alias claude='claude --dangerously-skip-permissions'`）仍然會經過啟動器，因為 shell 會在 `PATH` 上找 alias 裡的 `claude`；執行絕對路徑、或設了 `CLAUDE_CONFIG_DIR` 的 alias 則會繞過它。
- 它有成本：這樣啟動的每個 `claude` 都要先啟動 Python（約十分之一秒），額度讀數不新時查詢最多花 `hook_budget_seconds`。啟動器排在 session 的 `PATH` 最前面時，session 裡的派工也會經過它，它會立刻讓開。

`qr launch [--] <claude 參數>` 不透過啟動器做同樣的事。它的決定記在 `decisions.log`，來源是 `qr launch`。

## 會看到的訊息

以下都是附在 Bash 工具呼叫上、給主管 session 看的（`zh-TW` 介面）。

分流成功：

```
[帳號分流] 已自動改用 bob：週剩 88%（4.0 天後重置），跑 0、剛派 1
```

另一家族的狀況（有設定帳號、而且查得到時）：

```
另一家 Codex：carol 70%（預定 4.0 天後重置） 跑 0／dave 55%（預定 4.0 天後重置） 跑 0（數字即時）
```

Claude 派工沒寫 `--model`：

```
這條沒寫 --model，會用 Fable 額度
```

另一家族比較寬裕：

```
💡 Claude 全家最佳只剩 25%，Codex 的 carol 還有 70%。這件事若沒有品質上非用這家不可，建議改寫 codex exec 派過去。
```

全部都緊，但還是派了：

```
⚠ 全部都緊。最早預定重置：codex-1 共用週額度 10-15 21:34（預定，不保證屆時可派）
```

你指定的帳號本來會被跳過：

```
[帳號分流] 照你指定的 bob 執行，但它週剩只剩 3%，低於 10% 底線不派。現在最寬裕的是：alice：週剩 41%（4.0 天後重置）
```

接續對話：

```
[帳號分流] 接續對話沿用原帳號 bob。
```

被擋，沒有帳號可派：

```
第 1 個派工（claude／other）被擋，整條指令未執行。
沒有可派帳號
claude-main：主管帳號只剩 15%，保留給主管
claude-b：週額度用完
最早預定重置：claude-b 共用週額度 10-15 21:33（預定，不保證屆時可派）
另一家 Codex：carol 70%（預定 4.0 天後重置） 跑 0／dave 55%（預定 4.0 天後重置） 跑 0（數字即時）
要硬派請明寫帳號（會扣點數的情況不適用）；仍有待辦時稍後再派一次，工具會重查
```

被擋，因為會花錢：見最上面的例子。

額度回來了：

```
🔄 bob 共用週重置了（照預定換窗，預定 10-15 21:33）：剩 4% → 100%，發生在 10-15 20:10 到 10-15 21:40 之間。非 Fable 派工可以多派到這個帳號；Fable 仍受專屬窗限制，並須確認五小時窗與主管保護門檻。
```

quota-router 自己出錯：

```
[帳號分流] 分流失敗（OSError），照原指令執行。
```

設定檔不存在或壞掉時什麼都不會附；指令照原樣執行，hook 只在 stderr 寫一行，例如 `quota-router: no config at …, run "qr init"`。

## 已知限制

總原則：**hook 看不到的，就不分流，也不會檢查會不會花點數。** 它讀的是 Bash 指令執行前的文字；它不跑殼、不展開變數、也不看腳本裡面。`claude`／`codex` 要出現在殼會去找指令的位置才算派工，前面可以有已知的包裝器（`env`、`timeout`、`nice`、`nohup`、`time`、`command`、`exec`、`sudo`、`caffeinate`、`stdbuf`、`setsid`、`xargs`、`parallel`、`find -exec`）。

| 你寫的 | 會發生什麼 | 改成這樣 |
|---|---|---|
| `claude -p x --resume=ID`（等號） | 當成新派工，可能被換到別的帳號，而那裡沒有這段對話。 | 用空格：`--resume ID` |
| `codex exec <旗標> resume ID`（中間夾旗標） | 當成新派工，可能被換到別的帳號。 | 自己寫明原帳號：`CODEX_HOME=… codex exec …` |
| `claude -p x -c`、`codex exec resume --last`（不帶 id） | 用最近的對話檔猜帳號，可能猜到主管自己那段。 | `--resume <id>`／`resume <id>` |
| `bash -c '… claude -p …'`、`eval` | 看不到：不分流、也沒有任何提醒。 | 在字串裡自己呼叫 `qr pick <家族> --env` |
| `python3 wrap.py -- claude -p x`、`./run.sh claude -p x`（別的程式的參數） | 看不到：hook 無從得知那個程式會不會執行它的參數。 | 在包裝腳本裡呼叫 `qr pick`，或自己寫明帳號 |
| `"/opt/My Tools/claude" -p x`（執行檔加了引號） | 看不到。 | 不加引號的路徑，或 `PATH` 上的 `claude` |
| `CLAUDE -p x`（大寫，而且沒有 `.exe`／`.cmd`） | 看不到。 | `claude` |
| `claude --resume ID`、`claude -c`（沒有 `-p`） | 不算派工：互動式的 session 不分流。 | 一次跑完的工作請加 `-p` |
| `claude -p x -- --help` | 程式名之後任何位置出現 `--help`，hook 就不處理，寫在 `--` 後面也一樣。 | 提示詞請加引號，不要讓 `--help`／`--version` 成為獨立的字 |
| `codex exec resume --model m ID`（選項寫在 id 前面） | 選項的值會被當成對話 id。 | id 寫在前面：`codex exec resume ID --model m` |
| `false && export CODEX_HOME=…; codex exec x`、寫在 `if` 裡的 `export` | `export`／`unset` 是否真的執行，工具不判斷。可能把沒生效的當成有寫帳號，或反過來。 | 把帳號寫成那條派工自己的前綴 |
| `>log claude -p x`（重導向寫在前面） | 看不到：不分流。 | 重導向寫在指令後面 |
| `for … do claude -p …; done`、`xargs` | 只當成一次派工：整批落在同一個帳號，只計一次。 | 在迴圈裡每次呼叫 `qr pick` |
| `$(brew --prefix)/bin/claude -p x` | 前綴會被插兩次，指令壞掉。 | 直接寫 `claude` 或絕對路徑 |

另外：

- 只接了 Claude Code 的 Bash 工具。用內建 Agent／Task 工具或 workflow 起的子代理跑在主管自己的帳號；這個帳號由[啟動器](#開-session-也會挑帳號)在 session 開始時挑，之後不會再變。寫在腳本裡面的派工不在指令文字裡，所以不分流、也不檢查（請在腳本裡用 `qr pick`）。
- 純粹是資料的文字不會被動到：引號裡的字串、註解、heredoc 的內容（不管分隔詞是什麼）。所以寫在餵給殼的 heredoc 裡的派工（`bash <<EOF … EOF`）同樣看不到。
- 一條 Bash 指令只能整條放行或整條擋下。`codex exec a; claude -p b` 裡的 Claude 派工被擋時，Codex 那一段也不會執行。
- 只處理週額度、五小時窗、以及一個模型專屬的週額度。
- `qr install` 會用標準 JSON 格式重寫 `settings.json`。內容、鍵的順序、縮排寬度都保留；手動對齊或寫成一行的物件會被重新換行。備份檔是原封不動的原檔。
- macOS 與 Linux 上，目錄名稱含空格的帳號算不到它正在跑的任務（程序的環境變數在那裡沒辦法可靠地切開）；預約照算。
- `qr status` 與摘要行顯示的是數字的新舊。「即時」指最近幾分鐘內讀到的，不一定是這一次呼叫才查的。
- `qr check codex <數字>` 的意思是「設定檔裡第幾個 Codex 帳號」，調換順序後意思會變。寫在腳本裡請用 key。

## 平台差異

| | macOS | Linux | Windows |
|---|---|---|---|
| 設定檔 | `~/.config/quota-router/config.toml` | 同左 | `%APPDATA%\quota-router\config.toml` |
| 狀態目錄 | `~/.local/state/quota-router`（或 `$XDG_STATE_HOME`） | 同左 | `%LOCALAPPDATA%\quota-router` |
| Claude 登入憑證來源 | 鑰匙圈，沒有就讀 `<目錄>/.credentials.json` | `<目錄>/.credentials.json` | `<目錄>\.credentials.json` |
| Codex 登入憑證來源 | `<目錄>/auth.json` | 同左 | 同左 |
| 執行中任務怎麼算 | `ps`，連同每個任務的環境變數 | 同左 | PowerShell `Get-CimInstance Win32_Process`，只看得到命令列 |
| 指令在哪個殼執行 | `sh`／`zsh` | `sh`／`bash` | Git Bash |
| `claude` 啟動器 | `~/.local/share/quota-router/bin/claude`（或 `$XDG_DATA_HOME` 底下） | 同左 | `%LOCALAPPDATA%\quota-router\bin\claude.cmd`，未實測 |

測試在三個平台的 CI 上都會跑。實際日常使用目前只有 macOS，Windows 是驗證最少的一個。

- **macOS。** 第一次讀鑰匙圈時系統可能跳出授權詢問。
- **Windows：只處理 Git Bash。** Claude Code 經由 PowerShell 工具執行的指令不會被分流。
- **Windows：忙碌數比較不準。** Windows 不讓一個程序讀另一個程序的環境變數，所以執行中的任務只有在它自己或上層程序的命令列上看得到 `CLAUDE_CONFIG_DIR=…`／`CODEX_HOME=…` 時，才算得出是哪個帳號。hook 分流到「有寫目錄」的帳號的任務符合。靠先前 `export` 取得帳號的任務算不到；被分流到預設位置的 Claude 任務也算不到，因為那種寫法是 `env -u CLAUDE_CONFIG_DIR`，命令列上沒有目錄。最近五分鐘內的預約三個平台算法相同。
- **Windows：列程序要啟動 PowerShell**，大約吃掉 hook 時間預算裡的 0.5 到 2 秒；超過 2 秒就當作沒有人在忙。
- **Windows：路徑**寫進指令時一律用正斜線（`C:/Users/me/.claude-b`）。你自己寫的指令與設定檔則 `C:\…`、`C:/…`、`~/…`、以及 Git Bash 的寫法 `/c/…` 都認得；設定檔載入時會把 `/c/…` 轉成磁碟機路徑（只在 Windows）。
- **Windows：存在別處的登入讀不到**，包含 `%APPDATA%\Anthropic` 底下的 Console 登入，以及存在系統憑證庫裡的 Codex 登入。
- **Windows：啟動器未實測。** `qr install` 會寫一個批次檔 `claude.cmd` 來跑 `qr launch`；在 Windows 上 `claude` 是以子程序執行，而不是取代原程序。邏輯有測試涵蓋，但這個檔案沒有在真的 Windows 機器上跑過。

## 隱私與安全

**讀什麼**（全部唯讀）：

- Claude Code 的 OAuth token：macOS 上透過 `security find-generic-password` 讀登入鑰匙圈，其餘情況讀 `<目錄>/.credentials.json`。同一個鑰匙圈服務名稱底下可能有不只一筆（一筆只有 MCP 登入、一筆才是 Claude 登入），所以先問登記在你使用者名稱下的那一筆，再問該服務的任一筆，最後才讀檔案。
- `<目錄>/auth.json` 裡的 Codex token。
- 查詢失敗時的退路：Claude Code 存在 `.claude.json` 裡的用量，以及 Codex 的 `<目錄>/sessions/**.jsonl` 最後一筆 `rate_limits`。為了找到那一筆，最多會逐行讀最近的 20 個 session 檔；只留下那一筆。
- 各帳號目錄裡對話檔的檔名與修改時間，用來找出接續的那段對話存在哪個帳號。透過啟動器執行 `claude --continue` 時，也會讀目前目錄那個專案資料夾裡對話檔的前幾行，只用來分辨是 `claude -p`、子代理還是互動 session 留下的。
- 程序列表，用來數每個帳號正在跑的 `claude`／`codex` 任務。這表示會讀到執行中程序的命令列（裡面可能有提示詞），macOS 與 Linux 上還包含 `claude`／`codex` 程序的環境變數。實際用到的只有程式名稱與 `CLAUDE_CONFIG_DIR`／`CODEX_HOME`，其餘都不儲存。
- 只有 `qr doctor`：你的 shell 啟動檔（`~/.zshrc`、`~/.bashrc` 等幾個），用來找叫 `claude` 的 alias 或函式。只會顯示那幾行，看起來像憑證的內容會遮掉，什麼都不儲存。

quota-router 自己不會刷新、改寫或另存任何 token。

**送到哪裡。** 每個 token 只送到自己原廠的用量端點，別無他處。HTTP 重導向一律不跟，遇到重導向就當作查詢失敗：

- `https://api.anthropic.com/api/oauth/usage`
- `https://api.anthropic.com/api/oauth/profile`
- `https://chatgpt.com/backend-api/wham/usage`

quota-router 自己沒有遙測，也不發其他請求，除非你改了 `[endpoints]`。有一個副作用要知道：Codex 的即時查詢失敗、而本機數字又超過兩小時時，會在背景跑一次 `codex exec` 空指令（提示詞是「Reply with the single word: ok」、唯讀沙箱、低推理強度）來更新數字。那是真的 Codex CLI 在那個帳號上執行：它會連到原廠、可能刷新自己的登入、也會寫一個 session 檔，這些都不在上面的保證範圍內。它用的是那個帳號的 Codex 設定所選的模型，所以花多少額度看那份設定；很少，但不是零。已知帳號有點數、而訂閱額度用完、快用完或不明時不會啟動；點數餘額本身不明時則會。每個帳號每兩小時最多一次，`QR_NO_PROBE=1` 可以關掉。

**寫什麼**（都在狀態目錄裡）：

- 狀態檔：用量暫存、預約、用量與重置歷史、`qr install` 裝了什麼的紀錄，以及給 `--continue` 用的、透過啟動器開過的 session（工作目錄的雜湊、帳號 key 與時間；不存路徑）。裡面有帳號信箱、token 的短雜湊（不是 token 本身）、最近預約的 session id。
- `decisions.log`：每次決定一行，記時間、主管的帳號 key、session id 的前 8 個字元（或它的雜湊）、選中的帳號 key、家族、決定的種類、額度數字、模型類別、指令的 12 字元雜湊。不記指令原文、提示詞、信箱與目錄。
- `hook-errors.log`：每個錯誤一行，記時間、作業名稱、例外類別、程式位置、相關的帳號 key 或狀態檔。不記例外訊息與錯誤堆疊，因為那些內容可能引用到請求標頭與回應內容。
- `probe-<帳號>.log`：背景探測時 `codex` CLI 印出來的東西。
- `*.lock` 檔，以及短暫存在的 `*.tmp` 檔。

狀態目錄以外，它只寫 `qr install` 加的東西：你安裝的每個 `settings.json` 裡的 hook 條目，以及 `claude` 啟動器這個檔案。
- `debug.log`，**只有設了 `QR_DEBUG=1` 才寫**：完整的錯誤堆疊。寫入前會做一次樣式遮罩，但只是盡力而為；這個檔可能含有 token、標頭、回應內容與設定值。請當成敏感資料，用完就刪。

兩個紀錄檔的逐欄位格式在 [docs/REFERENCE.md](docs/REFERENCE.md)（英文）。

**hook 會改寫指令。** 這是它的功能，也是它的風險。一個在每條 Bash 指令之前執行、而且能改指令的程式，安裝它等於給了它很大的權限。它實際做的事很窄：在 `claude` 或 `codex` 的呼叫前面插入一段環境變數前綴，或者擋下整條指令；指令的其他部分不動。但有錯誤時仍可能把工作派到錯的帳號，或把它看錯的指令弄壞（見[已知限制](#已知限制)）。頭幾次派工後請用 `qr decisions` 核對。另外，能改設定檔的人就能新增帳號或改 `[endpoints]`，也就能把你的 token 導到別處，請把設定檔當成和 `settings.json` 一樣重要來保護。

## 免責聲明

- quota-router 使用兩家廠商**沒有公開的用量端點**。這些端點可能無預警變更或消失，屆時工具會讀不到額度，直到更新為止。
- 本專案**與 Anthropic、OpenAI 無關**，未經其認可，也不受其支援。
- 同時使用多個訂閱帳號是否符合各家的服務條款，請自行確認。
- **使用風險自負。** 本軟體依現狀提供，不附任何擔保（見 [LICENSE](LICENSE)）。

## 移除

```sh
qr uninstall                              # 裝過的每個 --config-dir 各跑一次
pip uninstall quota-router
```

`qr uninstall` 只移除 `qr install` 寫的那一條，認的是完全相同的指令與參數列表；手寫的、另一份安裝留下的、或只是參數裡提到這個套件的條目都不動。沒有 `--config-dir` 時，啟動器檔案內容和 `qr install` 寫的完全相同也會一併移除（`--no-launcher` 則保留；`--config-dir` 永遠不動它）。判定規則在 [docs/REFERENCE.md](docs/REFERENCE.md)。設定檔、狀態目錄、啟動器留下的空目錄，以及你 shell 啟動檔裡的 `PATH` 那一行會留著，要不要刪由你決定。

## 常見問題

**為什麼不在 Claude 和 Codex 之間自動切換？**
兩個 CLI 的參數不同，同一段提示詞交給兩邊做出來的東西也不一樣。一件工作該用哪一家是品質上的判斷，只有主管 session 或你自己能下。所以 hook 在派工時一併回報另一家族的額度，明顯比較寬裕時特別提醒，但不動指令。

**為什麼預設擋下付費點數？**
因為這種失誤沒有聲音。有點數的帳號在訂閱額度用完後照樣能跑，然後向你收錢，代理沒有理由會注意到。花錢應該是你針對那件工作點過頭的事。把 `QR_ALLOW_CREDITS=1` 寫在某一條指令前面，同意就是明確的、只限那一條、而且留在對話紀錄裡。真的不想要這層檢查，可以在設定檔寫 `allow_credits = true`。放行的方式只有這兩個。

**為什麼 Claude 的派工要寫明 `--model`？**
沒寫的話 CLI 會用該帳號的預設模型，而那可能正是有專屬（而且比較小的）週額度的模型。hook 無從得知，只能當作會用到那個模型、去檢查那份額度：結果可能是被擋、或被派到比較差的帳號，其實換個便宜的模型根本沒問題。寫了 `--model opus`（或任何名稱）就會照正確的額度評分，子代理也不會不小心吃掉最稀缺的那一份。

**沒有 Codex 可以用嗎？**
可以。只列 Claude 帳號即可，`codex` 指令會原封不動。

## 開發

```sh
pip install -e . pytest
python -m pytest
```

測試會建立暫存的家目錄，並用假的網路、鑰匙圈與程序列表，不會讀真實帳號資料。`tests/test_fresh_machine.py` 在全新的虛擬環境裡把安裝流程走一遍，用量端點由本機的假服務代替。它在 Windows 上跳過，離線又沒有 `setuptools` 快取時也會跳過（設 `QR_E2E_ONLINE=1` 允許下載）；而且 hook 指令是測試自己啟動的，不能證明真的 Claude Code session 會接受那條設定。跳過的測試清單、紀錄檔格式與其他細節在 [docs/REFERENCE.md](docs/REFERENCE.md)（英文），各版本的內容見 [CHANGELOG.md](CHANGELOG.md)。

## 授權

MIT，見 [LICENSE](LICENSE)。
