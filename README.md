# horo-DCB

horo-DCB 是以 Python、`discord.py`、9Router 與 Docker Compose 組成的 Discord AI Bot。專案把 AI 對話、受限制的 Web Research、選用的頻道語意記憶、Server Activity、Discord 行事曆看板、臨時語音、Steam 限時免費通知，以及管理員控制台整合在同一個 Bot 內。

這個儲存庫的目標是：下載後先執行一次 `scripts/setup.sh` 完成本機初始化，再填入無法由專案代為建立的 Discord 與 AI Provider 憑證，即可用 Docker Compose 建置及啟動。

## 功能概覽

- **AI 對話**：只有使用者 `@Bot` 或直接回覆 Bot 時才呼叫 Chat 模型。
- **短期上下文**：每個頻道各自保留最近 50 則人類訊息與 Bot 最終回覆，僅存在記憶體。
- **受限制的 Agent 工具**：Steam 查詢、目前頻道資訊、Web Search、同次搜尋來源限定的 Web Fetch；啟用 Semantic Memory 後再加入目前頻道專用的歷史搜尋。
- **圖片輸入與搜尋圖庫**：支援 JPEG、PNG、WebP；搜尋圖片只使用通過公開 URL 檢查的遠端網址，不由 Bot 下載或保存。
- **Semantic Memory**：選用的 SQLite 語意索引；會將伺服器文字頻道與 Thread 的人類文字送往 Embedding Provider。
- **Server Activity**：選用的 Guild 活動 metadata 記錄器；資料只保存在本機，管理員可從 `/控制台` 查看活動狀態與摘要。
- **Discord 行事曆**：管理員可把一則持久 Components V2 看板綁定到指定文字頻道；看板可新增、編輯、瀏覽與重新整理 Discord Scheduled Events，`@Bot`／Reply Bot 也可用自然語言查詢或產生待確認的新增／修改草稿。
- **臨時語音**：使用者進入入口後建立個人語音頻道，空頻道自動刪除。
- **Steam 限時免費**：查詢目前 100% 折扣且原本付費的遊戲，可定時公開通知，並可從 `/控制台` 一次選擇最多 25 個要標記的通知身分組。
- **`/控制台`**：管理員專用、ephemeral 的 Discord Components V2 介面；可查看系統與 Activity 狀態、24 小時摘要及近期活動，也可重新同步臨時語音及手動查詢 Steam。

## 先決條件

開始前需要：

1. Docker Engine 或 Docker Desktop，並可使用 Docker Compose v2。
2. Discord Application 與 Bot Token，且已啟用 **Message Content Intent**。
3. 至少一個可由 9Router 使用的 AI Provider。
4. 要使用 Web Search／Fetch 時，需在 9Router 設定對應 Provider；預設 Provider ID 為 `tavily`。

啟用 `SERVER_ACTIVITY_ENABLED=1` 時，還必須在 Discord Developer Portal 啟用 **Server Members Intent**。

Discord Application 還應以 `bot` 與 `applications.commands` scope 邀請。最低權限依啟用功能而異：

| 功能 | 所需 Discord 權限 |
|---|---|
| AI 對話與控制台 | View Channels、Read Message History、Send Messages |
| Server Activity | View Audit Log |
| Discord 行事曆 | 看板頻道需 View Channels、Send Messages；Bot 另需 Create Events、Manage Events |
| 臨時語音 | Connect、Manage Channels、Manage Roles、Move Members、Mute Members、Deafen Members |
| Steam 自動通知 | View Channels、Send Messages；要讓 Bot 自動建立通知頻道時另需 Manage Channels；若選定身分組本身不可被提及，Bot 另需 Mention Everyone |

專案不要求 Bot 取得 Administrator 權限。執行 `/控制台` 與 `/行事曆` 管理面板的使用者必須是伺服器管理員；從公共看板或 AI 確認新增／修改活動的使用者必須具有 Manage Events 或 Administrator。

## 安裝

### 1. 初始化本機設定

下載或 clone 儲存庫後，在專案根目錄執行：

```sh
sh scripts/setup.sh
```

此腳本會：

- 檢查 Docker 與 Docker Compose v2；
- 由 `.env.example` 建立 `.env`，並加入選用的圖片搜尋 Provider 設定；
- 在本機產生 9Router 使用的四個隨機秘密值；
- 將 `.env` 權限設為 `600`；
- 保留既有 `.env`，重跑時不覆寫內容；
- 先驗證 Compose 設定是否可解析。

腳本不會也不能替你建立 Discord Token、AI Provider 帳號或 9Router API Key。

### 2. 先啟動 9Router

```sh
docker compose up -d --build 9router
```

管理介面預設只綁定本機：

```text
http://127.0.0.1:20128
```

使用本機 `.env` 內的 `INITIAL_PASSWORD` 完成第一次登入；不要把它貼到終端紀錄、Issue 或聊天內容。登入後完成：

1. 立即修改初始密碼。
2. 設定 AI Provider；需要 Web Research 時也設定 Search／Fetch Provider。
3. 建立 9Router API Key。
4. 建立或選擇 Bot 使用的 Model／Combo。預設 `.env.example` 使用 `horo-main`。

### 3. 填入外部憑證

編輯 `.env`，至少替換：

```dotenv
DISCORD_TOKEN=[REDACTED_SECRET]
NINEROUTER_API_KEY=[REDACTED_SECRET]
```

並確認以下值對應你的 9Router 設定：

```dotenv
NINEROUTER_MODEL=horo-main
NINEROUTER_WEB_SEARCH_PROVIDER=tavily
NINEROUTER_IMAGE_SEARCH_PROVIDER=
NINEROUTER_WEB_FETCH_PROVIDER=tavily
```

`NINEROUTER_IMAGE_SEARCH_PROVIDER` 留空時沿用一般 Search Provider。原生回傳圖片 URL 的 Provider alias（例如 Exa、Google PSE、Linkup、SearchAPI、You.com 或 SearXNG）可直接提供圖庫；圖片 Provider 呼叫失敗時，Bot 會重試一般 Search Provider。

若 Provider 沒有安全圖片欄位，例如 9Router `0.5.55` 的 Tavily，Bot 會跳過 Instagram 等社群個人／品牌動態牆，只讀取第一個一般內容來源。Fallback 只接受 `og:image`、`twitter:image`、JSON-LD、`image_src`、圖片標籤，或帶有明確替代文字的 Markdown 圖片；Logo、頭像、placeholder 與沒有內容關聯的裸圖片 URL 都不會顯示。仍找不到時才安全降級為純文字結果。

Provider 的 API Key 只設定在 9Router；不要放進 Bot 的 `.env`，也不要交給模型。

### 4. 驗證並啟動完整服務

```sh
sh scripts/check-env.sh
docker compose up -d --build
```

查看狀態與 Bot 紀錄：

```sh
docker compose ps
docker compose logs -f bot
```

`check-env.sh` 只檢查設定是否存在與格式是否合理，不會輸出秘密值。

## 新安裝的安全預設

`.env.example` 將會建立 Discord 頻道或把一般聊天送往 Embedding Provider 的功能預設關閉：

```dotenv
SEMANTIC_MEMORY_ENABLED=0
SERVER_ACTIVITY_ENABLED=0
TEMP_VOICE_ENABLED=0
STEAM_FREE_GAMES_ENABLED=0
AI_TEXT_DISPLAY_ENABLED=1
```

- `SEMANTIC_MEMORY_ENABLED=1`：開始擷取 Bot 可讀取之伺服器文字頻道與 Thread 的所有人類非空文字，建立永久語意索引。
- `SERVER_ACTIVITY_ENABLED=1`：在本機記錄 Discord 可觀察到的 Guild 活動 metadata；預設或缺少此旗標時停用。
- `TEMP_VOICE_ENABLED=1`：允許 Bot 建立、同步與刪除臨時語音頻道。
- `STEAM_FREE_GAMES_ENABLED=1`：啟動每 15 分鐘一次的 Steam 自動通知；關閉時 `/控制台` 的手動查詢仍可使用。
- `AI_TEXT_DISPLAY_ENABLED=0`：停用 AI 回覆的 Components V2 `TextDisplay`，改用 Discord 原生文字訊息。

既有部署若沒有明確設定原有功能變數，Compose 與應用程式仍保留舊版預設行為；新的 `SERVER_ACTIVITY_ENABLED` 缺少時則明確停用。新部署應以 `.env.example` 的明確值為準。

## 設定參考

| 變數 | 新安裝值／必要性 | 用途 |
|---|---|---|
| `DISCORD_TOKEN` | 必填，人工設定 | Discord Bot credential。 |
| `NINEROUTER_API_KEY` | 必填，完成 9Router 設定後人工建立 | Bot 呼叫本機 9Router。 |
| `NINEROUTER_MODEL` | `horo-main` | 9Router Model／Combo ID。 |
| `NINEROUTER_WEB_SEARCH_PROVIDER` | `tavily` | 一般文字 Search 使用的 9Router Provider alias。 |
| `NINEROUTER_IMAGE_SEARCH_PROVIDER` | 留空 | 圖片查詢專用 Provider alias；留空時沿用一般 Search Provider。 |
| `NINEROUTER_WEB_FETCH_PROVIDER` | `tavily` | 9Router Fetch Provider alias。 |
| `NINEROUTER_EMBEDDING_MODEL` | `gemini/gemini-embedding-2` | Semantic Memory 使用的 Embedding model。 |
| `NINEROUTER_EMBEDDING_DIMENSIONS` | `768` | 向量維度；變更後不能直接混用舊資料庫。 |
| `SEMANTIC_MEMORY_ENABLED` | 新安裝 `0` | 是否保存頻道語意索引並呼叫 Embedding Provider。 |
| `SERVER_ACTIVITY_ENABLED` | 新安裝 `0` | 是否在本機記錄 Discord 可觀察到的 Guild 活動 metadata。 |
| `TEMP_VOICE_ENABLED` | 新安裝 `0` | 是否建立、同步及刪除臨時語音頻道。 |
| `STEAM_FREE_GAMES_ENABLED` | 新安裝 `0` | 是否啟動 Steam 公開通知背景工作；不影響手動查詢。 |
| `AI_TEXT_DISPLAY_ENABLED` | `1` | 是否以 Components V2 `TextDisplay` 顯示 AI 回覆。 |
| `JWT_SECRET`、`INITIAL_PASSWORD`、`API_KEY_SECRET`、`MACHINE_ID_SALT` | `setup.sh` 本機隨機產生 | 9Router 自身秘密；不得跨部署重用或提交。 |

`NINEROUTER_URL` 由 Compose 固定為內部服務網址 `http://9router:20128/v1`，新安裝不需要自行設定。`NINEROUTER_IMAGE_SEARCH_PROVIDER` 可留空並回退到一般 Search Provider；原有功能旗標缺少或留空時使用向後相容預設，`SERVER_ACTIVITY_ENABLED` 缺少或留空時停用。Embedding model／dimensions 若明確存在但留空或格式不合法，`check-env.sh`／preflight 會拒絕。

## AI 與 Agent 行為

- 普通聊天會進入該頻道的短期 history，但不會觸發 Chat 模型。
- 直接 `@Bot` 或回覆 Bot 才觸發 Agent。
- 每位使用者有 5 秒冷卻；同一頻道的 AI request 依序執行。
- 每次 request 最多 3 次模型呼叫、合計 4 個工具呼叫，整體逾時 120 秒。
- Search 最多 2 次，Fetch 最多 2 次，Semantic Memory Search 最多 2 次；圖片來源 HTML fallback 最多使用其中 1 次 Fetch。
- 工具名稱、參數、結果長度與目標 URL 都在 Bot 本地驗證；9Router 只負責模型路由與工具協定轉譯，不直接執行 Bot 工具。
- Web Fetch 只能使用同一次 Web Search 實際回傳且通過安全檢查的完整相同 URL。
- 工具呼叫與結果只存在當次 Agent 流程，不寫入短期 history 或 Semantic Memory。
- Guild 已綁定行事曆時，AI 可讀取近期 Scheduled Events；只有具 Manage Events／Administrator 的發問者才會取得「提出新增／修改草稿」工具。
- Calendar AI 工具永遠不直接修改 Discord：模型只產生 request-local `CalendarDraft`，Bot 回覆確認按鈕；只有原發問者在 10 分鐘內按下確認、且當下仍有權限與有效綁定時，才由 Bot 本地重新驗證後寫入 Discord。
- 行事曆 V1 以 UTC+8 解讀 `YYYY-MM-DD HH:MM`；AI 每次 request 會收到 Bot 本地產生的目前 UTC+8 時間，日期或目標活動不明確時不得猜測。
- 同一頻道等待中的 request 使用收到該問題當下的 history 快照，不會讀入稍後才出現的「未來訊息」。

一般 AI 回覆優先使用 Discord Components V2 `TextDisplay`：每則最多 4,000 字元、最多 4 則。若送出失敗，尚未送出的部分會回退到原生文字訊息；所有路徑都停用 Mention 解析。模型已要求圖片但最終 `image_count=0` 時，Bot 會固定加上「本次沒有取得可直接顯示的圖片」提示，避免文字誤稱已附圖。需要 Calendar 人工確認時則使用原生文字訊息搭配確認／修改／取消按鈕；若確認介面無法送出，不會執行 Calendar mutation。

## Semantic Memory 與隱私

Semantic Memory 預設關閉。開啟前應先確認伺服器成員已知情，並決定資料保留政策與 Embedding Provider 的使用條款。

啟用後：

- Guild TextChannel 與 Thread 的所有人類非空文字都會先寫入本機 SQLite pending queue，再批次送往 9Router 的 Embedding Provider；不需要先 `@Bot`。
- 不記錄 DM、Bot、Webhook、純圖片／純附件、AI 回覆或工具結果。
- ready row 不保留完整訊息本文，只保留 Discord scope、作者顯示名稱、內容 hash、時間與向量；搜尋命中時再向 Discord 取得目前原文。
- 資料庫位於 `bot_data` Volume 的 `/app/data/semantic_memory.sqlite3`，檔案權限為 `600`，目前沒有固定到期時間。
- 訊息編輯、刪除、批次刪除、Channel／Thread 刪除與 Bot 離開 Guild 時會同步清理索引；即使 Embedding worker 暫時不可用，只要資料庫仍存在，刪除事件仍會嘗試清理。Bot 被移出 Guild 時也會清除該 Guild 的短期頻道記憶。
- Bot 離線期間可能漏掉 Discord Gateway edit/delete，因此搜尋會先取最多 25 個候選，再向 Discord 驗證，最後最多回傳 5 筆目前仍存在的結果。
- Embedding model 或維度與現有資料庫 metadata 不一致時會 fail closed，不會默默混用舊向量。
- 背景 worker 若非預期終止，Semantic Memory 會立即標記為不可用，不會繼續宣稱服務正常。

## Server Activity 與隱私

Server Activity 預設關閉。啟用後，Audit Log 與 Gateway 來源的 Guild 活動 metadata 會寫入 `bot_data` Volume 的 `/app/data/server_activity.sqlite3`；資料庫權限為 `600`、使用 WAL，固定保留 30 天，並以有上限的 queue 與單一 writer 寫入。

- 只記錄 Discord ID、事件類型、時間與安全 metadata；不保存訊息內容、編輯文字、附件 URL、原始 Gateway payload、Poll 文字、AutoMod matched content、Invite code、Webhook token 或 URL。
- 不監聽 Presence 或 Typing，也不會把資料送往 AI、9Router 或 Semantic Memory。
- Bot、Webhook 與 DM 的訊息內容不會記錄；已知且仍在快取中的 Bot／Webhook 訊息會略過。
- Bot 離線期間可能漏掉一般 Gateway 活動。

## 圖片處理

觸發 AI 的 Discord 訊息可附帶最多 4 張 JPEG／PNG／WebP：

- 單張最多 8 MiB，合計最多 16 MiB；
- MIME、檔名副檔名與實際 magic signature 必須一致；
- 目前訊息沒有圖片時，可繼承直接回覆之同頻道訊息的一層圖片；
- 圖片只用於當次多模態 request，不寫入 history 或 Semantic Memory。

Web Search 圖片使用 `NINEROUTER_IMAGE_SEARCH_PROVIDER` 指定的 Provider，先接受 9Router 公開 `/v1/search` 原生回傳的圖片欄位；圖片 Provider 失敗時重試一般 Search Provider。若仍沒有安全圖片，Bot 會跳過社群個人／品牌動態牆，最多使用 1 次 Fetch，對第一個一般內容來源請求最多 50,000 字元 HTML。只接受 Open Graph／Twitter metadata、JSON-LD、`image_src`、圖片標籤，或有明確替代文字的 Markdown 圖片；Logo、頭像、placeholder 與沒有上下文的裸圖片 URL 會被拒絕。頁面本文不交給模型，也不寫入任何記憶。最終最多顯示 4 張安全且去重的圖片。Bot 不下載或保存圖片、不把圖片 URL 加入 Fetch allowlist，找不到時保留純文字搜尋結果並明確提示沒有可直接顯示的圖片。

## 臨時語音

啟用 `TEMP_VOICE_ENABLED=1` 後：

1. Bot 首次尋找唯一的 `➕ 建立語音` 語音頻道；不存在時自動建立。
2. 綁定後只使用 Discord Channel ID，入口改名或移動分類不影響功能。
3. 使用者進入入口時，在同一分類建立 `▍使用者名稱 的語音-🔊`。
4. 同一建立者同時只保留一個臨時頻道；再次進入入口會移回既有頻道。
5. 空頻道立即刪除；Parent／Child 狀態持久保存於 `bot_data`。

功能關閉時，啟動流程、Discord voice-state 事件與控制台同步動作都不會建立或管理語音頻道。

## Steam 限時免費

啟用 `STEAM_FREE_GAMES_ENABLED=1` 後，Bot 啟動時檢查一次，之後每 15 分鐘直接查詢 Steam Store：

- 只接受 `type=game`、`is_free=true`、原價大於 0、`discount_percent=100`；
- 排除永久 Free-to-Play、DLC 與非 100% 折扣；
- 只發領取通知，不登入 Steam，也不替使用者領取；
- 通知頻道首次依名稱綁定，之後使用 Channel ID；
- `/控制台 → Steam 免費遊戲` 可用原生 Role Select 一次選擇 1 到 25 個通知身分組，也可一次清除全部設定；
- 公開通知只允許目前選定的 Role mentions 觸發通知，使用者、其他角色與 `@everyone` 一律不在 allowlist；
- 若任一選定角色不可被提及，Bot 必須具有 Mention Everyone，否則控制台會拒絕整次設定；
- 通知角色被刪除時只移除失效的角色，其餘角色與 Steam 遊戲通知仍正常；
- 同一輪活動只通知一次，活動離開搜尋結果後才解除去重。

自動通知關閉時，`/控制台` 的 Steam 手動查詢仍可使用，而且不會發送公開通知；通知身分組設定也不會在停用期間修改。

## Discord 行事曆

行事曆以 **Discord Guild Scheduled Events** 作為唯一活動資料來源；Bot 不保存第二份 Event 資料庫。Guild Administrator 執行：

```text
/行事曆
```

Bot 會開啟只有原管理員本人可操作的 ephemeral 行事曆管理面板。面板的文字頻道下拉選單可直接綁定或重新綁定看板；若已綁定，也可在同一面板按「重新整理看板」或「解除綁定」。綁定後 Bot 會在選定文字頻道建立一則持久 Components V2 看板，並只保存 `guild_id`、`channel_id`、`message_id` 到 `bot_data` 的 `/app/data/calendar_board.json`。活動資料平常直接讀取 `discord.py` 隨 Gateway `GUILD_CREATE` 與 Scheduled Event create／update／delete 事件維護的 `Guild.scheduled_events` 快取，不會在每次按鈕互動重新呼叫 Scheduled Events List REST。活動變更時 Gateway 會先更新快取，再由 Bot 編輯**同一則看板訊息**；看板被誤刪時會在仍有效的綁定頻道重建，解除綁定後則不會復活。

看板提供：

- **新增活動**：具有 Manage Events／Administrator 的使用者可填 Modal 建立 External Scheduled Event；欄位為名稱、開始時間、活動長度、地點、說明。
- **編輯活動**：從最多 25 筆／頁的選單選擇活動，再以預填 Modal 修改；V1 只修改尚未開始的 External Event。
- **瀏覽活動**：以 ephemeral 分頁查看近期活動，不改動公共看板。
- **重新整理**：直接用目前 Gateway 快取重畫同一則看板，不顯示「思考中」，也不額外呼叫 Scheduled Events REST。

時間輸入固定使用 `YYYY-MM-DD HH:MM` 並以 UTC+8 解讀；活動顯示則使用 Discord timestamp，因此各使用者的 Discord Client 會依自己的時區呈現。看板每逢 UTC+8 午夜也會刷新一次，確保月份自然翻頁；這不是固定間隔輪詢。

行事曆管理面板只有 Guild Administrator 可開啟；綁定／重新綁定使用面板內的文字頻道下拉選單，重新整理與解除綁定則使用同一面板的按鈕，不再提供三個 Slash 子指令。

AI 仍只在使用者 `@Bot` 或直接回覆 Bot 時啟動。已綁定行事曆的 Guild 中，可以問「這週有什麼活動」；具 Manage Events／Administrator 的使用者也可以說「明天晚上八點新增團練兩小時」或「把團練改到九點」。模型只能查詢活動或產生待確認草稿，真正的 Discord 建立／修改一定要由原發問者按確認，且確認當下會再次檢查綁定、權限與目標 Event。

V1 刻意不做週期活動、自訂提前提醒、Google Calendar／ICS、Voice／Stage Event 建立或修改、Event 刪除／取消、RSVP 管理與每 Guild 自訂時區。

## 管理控制台

`/控制台`：

- 只允許 Guild Administrator 使用；
- 回覆為 ephemeral，只有執行者看得到；
- 只編輯同一則 interaction response，不另發控制訊息；
- 顯示 AI、9Router、臨時語音與 Steam 狀態；Combo 由公開 `/v1/models` 確認，公開 metadata 沒有 Effort 時顯示「無法取得」但不誤報故障；
- 提供「伺服器活動」頁面，顯示狀態、queue／dropped、24 小時摘要與最多 10 筆近期活動，並可用全部／管理／成員／訊息／語音篩選；只有 Guild Administrator 可使用；
- 對停用功能顯示「停用」，不誤報為故障或未設定；
- 不提供 Provider credential、模型路由修改、任意 Discord 管理或 Semantic Memory 管理功能。

## 專案結構與責任

```text
Discord
  └─ src/bot.py                 Discord 事件與依賴組合
       ├─ src/chat.py           短期 history、冷卻、頻道鎖、Agent loop
       ├─ src/agent_tools.py    工具 schema、參數驗證與安全執行
       ├─ src/ai_client.py      9Router HTTP 邊界
       ├─ src/discord_images.py Discord 圖片驗證
       ├─ src/discord_output.py Discord 長訊息與 Components V2 輸出
       ├─ src/semantic_memory.py SQLite、Embedding worker、搜尋與刪除同步
       ├─ src/server_activity.py Guild 活動 metadata、保留與查詢
       ├─ src/calendar_events.py Discord Scheduled Events、持久看板、Modal 與 AI 確認
       ├─ src/temp_voice.py     臨時語音狀態與 Discord 行為
       ├─ src/steam_free_games.py Steam 查詢、狀態與通知
       └─ src/admin_panel.py    管理員控制台
```

`src/config.py` 是 runtime 設定的單一解析入口；`src/preflight.py` 只輸出不含秘密的安全摘要。各功能模組不直接讀取其他模組的內部欄位，外部服務也各有單一邊界。Semantic Memory 由 `src/bot.py::main()` 透過 constructor 明確注入 `AgentTools` 與 `HoroBot`，不使用二階段 attach 或跨物件反向尋找依賴。

詳細設計與刻意不做的範圍見 `horo-DCB.md`。

## Docker 設計

- Bot 映像使用 digest 固定的 Python 3.14 Alpine 基底；`requirements.txt` 是 Docker、CI、GitHub dependency graph 與 Dependabot 共用的唯一完整 runtime pin set；容器使用非 root 使用者、唯讀 root filesystem、`tmpfs /tmp` 與 `no-new-privileges`，建置完成後會移除 runtime `pip`。
- Bot 不公開主機連接埠。
- 9Router Dashboard 只綁 `127.0.0.1:20128`。
- `bot_data` 與 `9router_data` 使用 Docker Volume 持久保存。
- 兩個服務都有 log rotation 與停止寬限時間。
- 9Router healthcheck 使用 `/api/health`，必須回傳成功 HTTP 狀態與 `ok=true`。

### 9Router 固定版本與更新邊界

`9router/Dockerfile` 直接繼承官方 9Router `0.5.55` immutable image digest，不修改 Next.js 編譯產物，也不包含自製 bundle patch：

- Provider Dashboard Alias UI 使用 9Router 上游原生功能；
- Chat、Embedding、模型清單、Search 與 Fetch 只走公開 `/v1` API；
- Combo 由 `/v1/models` 確認，`/v1/models/info` 沒有 Combo 詳細資料時安全降級顯示設定 ID；
- 圖片搜尋只使用 Provider 經公開 `/v1/search` 原生回傳的圖片欄位，不再修改 Tavily normalizer。

因此一般升級只需更換官方 digest 並執行契約測試與隔離 Volume smoke test。測試失敗時只調整 `src/ai_client.py` 或 Provider 設定，不應再 patch `.next`、壓縮字串或 Webpack module。

## 開發與驗證

執行與 CI 相同的主要檢查：

```sh
docker compose config --quiet
docker compose build bot 9router
docker compose run --rm --no-deps bot python -m unittest discover -s tests -v
docker compose run --rm --no-deps bot python -m src.preflight
docker compose run --rm --no-deps -e PYTHONPYCACHEPREFIX=/tmp/pycache bot \
  python -m compileall -q src tests
```

`requirements.txt` 是唯一的 Python runtime dependency manifest，完整固定直接與傳遞依賴；Docker、CI 與 GitHub dependency graph 都讀取同一檔，GitHub 上的 Python dependency 更新則由 Dependabot 直接對這一檔提出變更。`docker compose build bot` 會在 image 建置階段執行 `pip check`；runtime image 不保留 `pip`。`src.preflight` 只顯示安全設定，不顯示 Discord Token、9Router API Key 或其他秘密。

更多開發規則見 `CONTRIBUTING.md`；安全通報與部署邊界見 `SECURITY.md`。

## 已知邊界

- 第一次啟動無法完全自動化：Discord Application、Message Content Intent、Bot 邀請、AI Provider 與 9Router API Key 都需要擁有者操作。
- Semantic Memory 不回溯舊 Discord 歷史、不搜尋其他 Channel／Guild，也不保證收到 Bot 離線期間的所有 Gateway edit/delete。
- Server Activity 可能漏掉 Bot 離線期間的一般 Gateway 活動；啟用時需要 Server Members Intent 與 View Audit Log。
- 目前向量搜尋是 SQLite 全量掃描後排序，適合目前規模；資料量與延遲有實測壓力前不引入外部 Vector DB。
- 不提供語音播放、錄音、語音辨識、任意 Shell、檔案系統、任意 HTTP、多 Agent 或 MCP。
- 搜尋圖片優先使用 Provider 原生圖片；HTML fallback 只接受有結構或明確圖說的圖片並跳過社群動態牆，因此寧可不顯示，也不保證每個網站或每次查詢都有圖。

## 授權

此儲存庫目前**沒有授權檔**。在擁有者明確選擇並加入 `LICENSE` 前，不應把它描述為開源專案，也不應假設外部使用者具有修改、散布或再授權權利。
