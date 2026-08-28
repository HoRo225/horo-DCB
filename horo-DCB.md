# horo-DCB 架構與設計邊界

本文件說明目前程式碼的架構原則、功能邊界、資料流、持久化、安全限制與驗證要求。實際行為以目前工作區程式碼與可重新執行的檢查為準。

## 1. 目標

horo-DCB 是單一 Discord Bot 應用程式，透過同一份 Docker Compose 使用本機 9Router：

```text
Discord Gateway
  -> horo-DCB Bot
       -> 9Router
            -> Chat / Embedding / Web Provider
```

Bot 提供：

- `@Bot`／Reply Bot 才觸發的 AI 對話；
- 有上限、每頻道隔離的短期上下文；
- 有明確 allowlist 與預算限制的 Agent 工具；一般工具維持唯讀，Calendar 寫入只允許模型產生草稿，真正 Discord mutation 必須經人類確認後由 Bot 本地執行；
- 選用的頻道 Semantic Memory；
- 選用的 Server Activity 稽核與一般活動統計；
- Discord 圖片輸入與搜尋圖片呈現；
- Discord Scheduled Events 行事曆與持久看板；
- 臨時語音；
- Steam 限時免費查詢與選用的公開通知；
- 管理員 `/控制台`。

## 2. 核心架構原則

### 2.1 單一組合入口

`src/bot.py::main()` 是 runtime composition root，也就是所有依賴實例化與接線的唯一入口。它先以 `AppConfig.from_env()` 解析一次設定，再把具體值傳入其他物件。選用的 Semantic Memory 也由這裡透過 constructor 同時明確注入 `AgentTools` 與 `HoroBot`；不使用二階段 attach，也不允許 `HoroBot` 從 `ChatManager` 或 `AgentTools` 反向尋找隱藏依賴。

`ServerActivityMonitor` 只在 `config.server_activity_enabled` 為真時建立，並明確注入 `HoroBot`。`AdminPanelView` 只取得同一個 monitor 作為唯讀依賴；它永遠不注入 `AgentTools`、`ChatManager`、AI、9Router 或 `SemanticMemory`。

共享 `AIClient` 的生命週期由 runtime owner `HoroBot` 明確管理；`ChatManager` 與 `AgentTools` 只把它當成 consumer 使用。

功能模組不各自重複讀取環境變數。這避免同一設定在匯入時、啟動時與測試時產生不同結果。

### 2.2 外部服務各有單一邊界

- Discord Gateway／Interaction：`src/bot.py`
- 9Router HTTP 與 Web 回應正規化成專案自有型別：`src/ai_client.py`
- Discord Attachment：`src/discord_images.py`
- Discord 輸出限制與 Components V2：`src/discord_output.py`
- Discord Scheduled Events／行事曆看板：`src/calendar_events.py`
- Steam Store HTTP：`src/steam_free_games.py`
- SQLite Semantic Memory：`src/semantic_memory.py`
- Discord Server Activity 與獨立 SQLite：`src/server_activity.py`

其他模組不直接複製這些協定或安全檢查。

### 2.3 功能模組擁有自己的狀態

- `ChatManager` 擁有短期 history、冷卻、頻道鎖與 Agent loop。
- `SemanticMemory` 擁有 schema、pending queue、Embedding worker、搜尋與刪除同步。
- `ServerActivityMonitor` 擁有自己的 SQLite schema、有界佇列、單一 writer、audit-entry 去重、固定 30 天保留、runtime status 與唯讀查詢。
- `CalendarManager` 擁有 Guild 看板綁定、Discord Scheduled Event 讀寫、持久 Components V2 看板、Calendar Modal／確認 UI、每 Guild 看板更新鎖與 UTC+8 午夜刷新生命週期；活動本身仍只存在 Discord。
- `TempVoiceManager` 擁有 Parent／Child 綁定與語音 reconcile。
- `SteamFreeGamesNotifier` 擁有通知頻道綁定、活動去重與輪詢生命週期。
- `AdminPanelView` 只讀取各模組公開摘要並呼叫既有公開動作，不建立第二套狀態。

### 2.4 信任邊界採 fail closed

外部輸入不符合預期時拒絕執行，而不是猜測：

- 工具名稱與 JSON 參數不合法時不呼叫外部服務；
- Web Fetch 目標不是同次 Search 接受的完整 URL 時拒絕；
- 圖片 MIME、副檔名與 magic signature 不一致時拒絕；
- Semantic Memory metadata 與模型／維度不相容時停用；
- Calendar 看板 state 損壞時停止寫入，不部分載入；舊看板的 Guild／Channel／Message ID 與目前綁定不一致時拒絕互動；AI Calendar draft 在確認時重新檢查原使用者、Guild、權限、綁定與 Event 狀態；
- 9Router 公開 API 回應不符合本地契約時拒絕使用，不猜測欄位；
- 功能旗標關閉時，事件與控制台動作都不能繞過。
- Server Activity 不保存 raw payload、訊息內容、附件 URL、Poll text、AutoMod matched text、Invite code 或 Webhook secrets；一般失敗與既有 Discord handlers 隔離，不能中斷原有事件處理；佇列已滿時只增加 dropped 計數，不阻塞 Gateway。

## 3. 技術選擇

- Python 3.14
- `discord.py==2.7.1`
- `aiohttp==3.14.3`
- Python 標準函式庫 `sqlite3`
- Python 標準函式庫 `unittest`
- Docker Compose v2
- 9Router 公開 `/v1` Chat、Embedding、Models、Search 與 Fetch API

目前刻意不加入 Redis、外部 Vector DB、OpenAI SDK、Gemini SDK、工作佇列框架、MCP 或多 Agent。現有標準函式庫與兩個直接 dependency 已足夠支撐目前需求。

## 4. 設定模型

`src/config.py::AppConfig` 是設定的單一應用程式模型：

- Discord credential；
- 9Router URL、API credential、Model／Combo；
- 一般 Search、圖片 Search 與 Fetch Provider alias；
- Embedding model／dimensions；
- Semantic Memory、Server Activity、臨時語音、Steam 自動通知、AI TextDisplay 功能旗標。

必要值必須存在且不能是範本 sentinel；布林值只接受文件列出的值；維度必須是正整數。

`src/preflight.py` 使用同一個 `AppConfig`，只輸出安全欄位，不輸出任何 credential。

### 4.1 新安裝預設

`.env.example` 明確設定：

```dotenv
SEMANTIC_MEMORY_ENABLED=0
SERVER_ACTIVITY_ENABLED=0
TEMP_VOICE_ENABLED=0
STEAM_FREE_GAMES_ENABLED=0
AI_TEXT_DISPLAY_ENABLED=1
```

Semantic Memory、Server Activity、臨時語音與 Steam 自動通知具有資料或 Discord 管理副作用，因此新安裝採 opt-in。既有功能的相容預設仍維持舊行為，避免升級時偷偷關閉；但 `SERVER_ACTIVITY_ENABLED` 不採用這個舊功能相容規則，缺少時固定為 `false`，必須明確設為 `1` 才啟用。

## 5. Discord 訊息資料流

每收到 Discord 訊息：

1. 忽略其他 Bot、Webhook 與不適用輸入。
2. Semantic Memory 啟用時，先把符合條件的 Guild TextChannel／Thread 人類非空文字寫入 SQLite pending queue；這一步不等待 Embedding Provider。
3. 判斷是否直接 `@Bot` 或直接回覆 Bot。
4. 未觸發的非空文字加入該 Discord Channel／Thread／DM 的短期 history，然後停止，不呼叫 Chat 模型。
5. 觸發者先驗證目前訊息或直接引用訊息的圖片，再把目前 turn 加入短期 history。
6. 在目前 turn 寫入後立即建立短期 history 快照。
7. 檢查每位使用者冷卻，並等待該頻道的 async lock。
8. 以第 6 步的快照執行 Agent，避免等待期間稍後到達的訊息被錯誤加入較早問題。
9. 送出實際成功的 AI 回覆，並只把成功顯示的內容加入短期 history。

同一頻道的 request 依序處理；不同頻道可獨立進行。

## 6. 短期 history

- 每個 Discord Channel／Thread 獨立保存。
- 上限 50 則人類文字與 Bot 最終回覆。
- 只存在記憶體，Bot 重啟後消失。
- 送給模型的近期內容最多約 16,000 字元。
- 超過上限時從最舊內容移除；最新單則本身超限時截斷保留。
- 工具呼叫、工具結果、圖片 Base64 與 Web Research context 不加入。
- Channel／Thread 刪除時清除對應 history 與 lock。
- Bot 被移出 Guild 時清除該 Guild 所有已知 Channel／Thread history。

## 7. Agent loop

`src/chat.py::ChatManager` 擁有 bounded agent loop：

- `MAX_AGENT_TURNS = 3`
- `MAX_TOTAL_TOOL_CALLS = 4`
- `AGENT_TIMEOUT_SECONDS = 120`
- Search 每次 request 最多 2 次
- Fetch 每次 request 最多 2 次
- Semantic Memory Search 每次 request 最多 2 次

同一模型回合的工具依回傳順序逐一執行。若一整批工具會使總數超過 4，該批全部不執行；第三次模型呼叫仍要求工具時也不執行。

每次 `generate_reply()` 都建立新的 `ResearchContext`，只在當次 request 保存：

- Search results 與本地 `source_id`；
- 可供 Fetch 的完整 URL allowlist；
- Search／Fetch／Memory Search 呼叫次數；
- 最多四張安全搜尋圖片。

這些資料不跨 request 共用。

## 8. Agent 工具

基礎 Agent 提供 Steam、目前頻道、Web Search、Web Fetch 四個工具；啟用 Semantic Memory 時再加入 `search_channel_memory`。Guild 綁定 Calendar 看板後才動態加入 Calendar 工具，且依發問者當下權限縮小 schema：一般成員只有查詢，具 Manage Events／Administrator 才可提出新增／修改草稿。

### 8.1 `get_steam_free_games`

重用 `SteamFreeGamesNotifier.fetch_current_offers()`。只查詢，不登入、不領取、不公開通知，也不修改 Discord。

### 8.2 `get_current_channel_info`

只回傳 Bot 本地建立的 `ToolContext` 顯示資訊：Guild 名稱、Channel 名稱與類型。模型不能指定 Guild／Channel ID。

### 8.3 `web_search`

模型只能提供：

- 1 至 300 字元查詢；
- `web` 或 `news` 類型；
- 選用的 `include_images` 布林值。

Provider alias、HTTP headers、API credential、結果上限與 model 不在 schema。一般查詢固定使用 `NINEROUTER_WEB_SEARCH_PROVIDER`；`include_images=true` 時改用 `NINEROUTER_IMAGE_SEARCH_PROVIDER`，留空則沿用一般 Provider。兩者都只呼叫 9Router 公開 `/v1/search`。圖片 Provider 呼叫失敗時只重試一般 Search Provider；Bot 最多保留五筆通過安全檢查的來源。

圖片先從 Provider 原生回傳的 `metadata.image_url` 或相容 `images` 欄位取得。若沒有安全圖片，`AgentTools` 會排除社群個人／品牌動態牆，最多使用一次同一個 ResearchContext 的 Fetch 額度，只對第一個一般內容來源呼叫公開 `/v1/web/fetch`，讀取最多 50,000 字元 HTML。只接受 Open Graph／Twitter metadata、JSON-LD、`image_src`、圖片標籤，或具有明確替代文字的 Markdown 圖片；Logo、頭像、placeholder 與沒有上下文的裸圖片 URL 會被拒絕。頁面本文不交給模型、不加入 history 或 Semantic Memory。仍無圖片時回傳 `image_count=0`，`ChatManager` 再固定加上沒有可直接顯示圖片的提示。整個流程不使用 Tavily 私有欄位，也不修改 9Router bundle。

### 8.4 `web_fetch`

模型只能提供 URL。該字串必須完整等於同一次 request 的 Search result URL，且通過 `http`／`https`、hostname 與公開 IP literal 安全檢查。模型使用的 Fetch 文字最多 15,000 字元；圖片 HTML fallback 最多 50,000 字元且每次 request 最多使用一次，兩者仍共用總計兩次 Fetch 的上限。

Bot 不做一般網域 DNS resolution，也不自行追蹤重新導向；實際抓取由 9Router Provider 負責。

### 8.5 `search_channel_memory`

模型 schema 只有 1 至 300 字元的 `query`。目前 Channel／Thread ID 由 Bot 內部 `MemoryScope` 強制提供，不能跨 scope 搜尋。

### 8.6 `calendar_get_events`

只在目前 Guild 已綁定 Calendar 看板時提供。模型可選擇 1 至 100 字元的 `query`，最多取得 25 筆近期 Discord Scheduled Events。真正 Discord Event ID 不回傳模型；`ResearchContext` 只在當次 request 建立 `event_ref -> event_id` 對照，模型後續編輯只能引用這個 `event_ref`，不能跨 request、跨 Guild 指定任意 Event ID。

### 8.7 `calendar_propose_create`

只對具 Manage Events／Administrator 的發問者提供。schema 只允許名稱、`YYYY-MM-DD HH:MM` 開始時間、1 至 10080 分鐘活動長度、地點與選用說明。工具只在 Bot 本地建立通過驗證的 `CalendarDraft` 並回傳 `requires_confirmation=true`；不呼叫 Discord create API。

### 8.8 `calendar_propose_edit`

同樣只對具 Manage Events／Administrator 的發問者提供，而且必須引用同次 `calendar_get_events` 回傳的 `event_ref`。Bot 重新取得目前 Event，V1 只接受 External + Scheduled，將現有資料與指定修改合併成完整 `CalendarDraft`；工具本身不呼叫 edit API。

Calendar draft 不寫入短期 history、Semantic Memory 或磁碟，也不跨 Bot restart 恢復。`ChatReply` 若帶 draft，Discord adapter 會顯示只有原發問者可操作的確認／修改／取消介面，10 分鐘後失效；真正 mutation 前重新檢查 Guild 綁定、使用者 Manage Events／Administrator、Bot 權限與目標 Event 狀態。這是原本「Agent 不直接修改 Discord」邊界的受控擴充：**模型只有 read + propose，Discord mutation 仍是人工確認後的本地程式行為。**

所有工具結果都視為不可信資料，不能覆寫 system rule、索取秘密、授權新工具或提升權限。

## 9. Semantic Memory

### 9.1 擷取範圍

啟用後只記錄功能上線後的新訊息：

- Guild TextChannel／Thread 的人類非空文字。

不記錄：

- DM；
- Bot／Webhook；
- 純圖片／純附件；
- AI 回覆；
- Agent 工具呼叫與結果。

### 9.2 寫入與 Embedding

1. Discord event 先把本文寫入 SQLite pending row。
2. 背景 worker 每批最多 16 則呼叫 9Router `/v1/embeddings`。
3. 成功後保存 L2 normalized float32 vector，並把 `pending_text` 清為 `NULL`。
4. 失敗 row 保留並做退避重試；永久失敗的單筆不阻塞後續 row。
5. 背景 worker 發生未預期錯誤時，守護層將 `available` 設為 `False` 並記錄錯誤；呼叫端隨即 fail closed。

ready row 只保留 message／guild／channel scope、作者顯示名稱、內容 hash、時間與向量，不永久保存完整本文副本。

### 9.3 搜尋與 Discord 驗證

目前搜尋流程：

1. 只讀取目前 Channel／Thread 的 ready vectors。
2. 以 cosine similarity 排序，先取最多 25 個候選。
3. 逐筆向 Discord `fetch_message()` 取得目前原文。
4. 訊息不存在時刪除索引。
5. 暫時無權限或 HTTP 失敗時略過，不刪除索引。
6. 內容已編輯時重新 Embedding、更新索引並重新計分。
7. 最多回傳 5 筆已驗證結果，每筆本文最多 1,000 字元。

SQLite 目前使用全量 vector scan。沒有實際資料量或延遲證據前，不引入外部 Vector DB；需要擴充時先考慮同一 SQLite 邊界內的成熟 vector extension，再考慮獨立服務。

### 9.4 刪除同步

會處理：

- raw message delete；
- raw bulk delete；
- message edit；
- Channel delete；
- Thread delete；
- Guild remove。

一般搜尋與寫入在 `available=False` 時 fail closed；刪除不同，只要資料庫檔仍存在就會繼續嘗試，以免 Embedding worker 或模型 metadata 問題連帶阻止資料清理。底層資料庫真的不可讀時，Discord event adapter 只記錄安全錯誤，不向使用者洩漏內部細節。

Bot 離線期間可能漏掉 Gateway edit/delete，因此搜尋時仍必須做 Discord source-of-truth 驗證。

### 9.5 資料與相容性

- DB：`/app/data/semantic_memory.sqlite3`
- Docker Volume：`bot_data`
- 檔案 mode：`600`
- journal mode：WAL
- schema／Embedding model／dimensions 寫入 metadata
- metadata 不相容時 fail closed
- 目前沒有固定到期時間

## 10. Server Activity

Server Activity V1 使用兩個權威來源：Discord Audit Log 負責可追溯管理者的 actor／action，Gateway events 負責一般活動。資料採 ID-first 儲存，只保存辨識與統計必要的 Discord ID、事件類型、時間與安全的結構化欄位，不複製原始事件內容。

一般訊息活動只記錄 Guild 內的人類訊息；Bot、Webhook 與 DM 不記錄。對 cache 已能判定為 Bot 或 Webhook 的訊息立即略過；功能不訂閱或記錄 Presence、Typing。啟用時 `intents.members=true`，`intents.presences=false`；Bot 必須在 Developer Portal 開啟 Server Members Intent，並在 Guild 具備 View Audit Log 權限。Bot 離線期間，普通 Gateway activity 存在無法回補的缺口；管理事件仍以 Audit Log 為準，不把 Gateway 推測成 actor。

- DB：`/app/data/server_activity.sqlite3`
- Docker Volume：`bot_data`
- 檔案 mode：`600`
- journal mode：WAL
- 固定保留 30 天，非可調式永久歷史
- 有界佇列與單一 writer；Audit Log entry 依 ID 去重
- `AdminPanelView` 只透過 monitor 的 public query API 讀取狀態、摘要、近期紀錄與篩選結果
- 不提供 AI 或 Agent 工具，不注入 `AgentTools`、`ChatManager`、9Router 或 `SemanticMemory`

## 11. Discord 圖片

### 11.1 使用者附件

- JPEG／PNG／WebP
- 最多 4 張
- 單張最多 8 MiB
- 合計最多 16 MiB
- MIME、副檔名與 magic signature 三者驗證
- 可繼承直接回覆之同頻道訊息的一層圖片
- 目前訊息圖片優先，不與引用圖片自動合併
- 不跨 Channel、不追多層 reply chain
- 只存在當次多模態 request，不寫入任何記憶

### 11.2 Web Search 圖片

- `include_images=true` 時使用獨立的圖片 Search Provider；未設定時沿用一般 Search Provider
- 圖片 Provider 呼叫失敗時，重試一般 Search Provider
- 先接受 9Router 公開 `/v1/search` 原生回傳的圖片欄位
- 原生沒有安全圖片時，跳過社群個人／品牌動態牆，最多用一次 Fetch 讀取第一個一般內容來源的 HTML
- 只接受 Open Graph／Twitter metadata、JSON-LD、`image_src`、圖片標籤與有明確替代文字的 Markdown 圖片；拒絕 Logo、頭像、placeholder 與沒有上下文的裸圖片 URL
- 最多四個公開、安全、去重的遠端 URL
- Provider 與來源頁面都沒有圖片時安全降級成純文字來源，並由 Bot 固定顯示沒有可直接顯示圖片的提示
- 不下載、不 HEAD／GET 或保存圖片本身；HTML 只由既有 9Router Fetch Provider 取得
- 圖片 URL 不加入 Fetch allowlist
- 只放在第一個 Components V2 回覆 chunk 的 `MediaGallery`
- `AI_TEXT_DISPLAY_ENABLED=0` 時不建立圖庫

## 12. Discord 回覆

`src/discord_output.py` 集中管理分段與 Components V2：

- TextDisplay 每則最多 4,000 字元、最多 4 則；
- 原生文字每則最多 2,000 字元、最多 8 則；
- 優先依段落、換行與空白切分；
- 跨訊息補齊 fenced code block；
- TextDisplay 部分失敗時只回退尚未送出的內容；
- history 只記錄實際成功送出的內容；
- AI 與管理控制台路徑使用 `AllowedMentions.none()`；Steam 公開通知若設定通知身分組，只對目前選定的 Roles 使用 explicit allowlist，使用者、其他角色與 `@everyone` 仍禁止通知。

## 13. 臨時語音

啟用時：

- 首次尋找唯一的 `➕ 建立語音`，找不到則建立；
- 綁定後只使用 Channel ID；
- 使用者加入入口時建立 `▍使用者名稱 的語音-🔊`；
- 同一 Guild／Owner 最多一個 Child；
- 建立者取得改名、人數上限與成員控制所需的頻道權限；
- Bot 重啟後 reconcile 既有 Parent／Child 與停留在入口的成員；
- 空 Child 立即刪除；
- 狀態以嚴格正整數 ID 保存於 `bot_data`。

功能旗標關閉時：

- 啟動不 reconcile；
- voice-state event 不建立／刪除；
- `/控制台` 顯示停用；
- 同步按鈕停用；
- 即使收到手工構造的 action，也不呼叫 reconcile。

## 14. Steam 限時免費

啟用自動通知時：

- Bot 啟動後立即檢查一次，之後每 15 分鐘一次；
- 每批 Search 與 appdetails 共用 90 秒 timeout；
- 僅接受 `type=game`、`is_free=true`、原價大於 0、`discount_percent=100`；
- 排除 Free-to-Play、DLC 與非 100% 折扣；
- 頻道首次依唯一名稱綁定，之後使用 Channel ID；
- `/控制台` Steam 頁使用 Discord 原生 `RoleSelect` 一次設定 1 到 25 個通知身分組；version 1 state 以 `role_ids` 清單保存，新版仍相容舊的 optional `role_id`，讀取後不需額外 migration；
- `@everyone`／Guild default role 永遠拒絕；任一選定角色若本身不可被提及，設定時要求 Bot 具有 `Mention Everyone`，否則整次設定 fail closed；
- Components V2 `TextDisplay` 內才放入選定 Role mentions，`allowed_mentions` 只 allowlist 當次成功解析的這些 Roles，不開放 users、其他 roles 或 everyone；
- 角色不存在時只從 `role_ids` 移除失效項目，其餘角色與 Steam 遊戲通知仍照常；角色暫時不可被提及時只略過該角色的 ping，不阻止其他角色或遊戲通知；
- 發送成功後才標記通知；
- 活動離開搜尋結果後才解除去重；
- 不登入、不自動領取。

自動通知關閉時，手動 `fetch_current_offers()` 仍供控制台與 Agent 使用，且不產生公開訊息；控制台也不允許修改通知身分組。

## 15. Discord 行事曆

### 15.1 Source of truth 與綁定狀態

Discord Guild Scheduled Events 是唯一活動資料來源。`CalendarManager` 不建立 Event 資料庫、不複製活動本文；本地只在 `/app/data/calendar_board.json` 保存每個 Guild 的 `guild_id`、`channel_id`、`message_id`，使用 versioned JSON、暫存檔原子替換與 mode `600`。狀態檔無效時 fail closed，不載入部分 binding。

`/行事曆` 是唯一的管理員 Slash Command，只允許 Guild Administrator。它會開啟 15 分鐘 timeout 的 ephemeral `CalendarAdminView`，且每次 component interaction 都重新要求「原開啟者 + 原 Guild + 目前仍是 Administrator」。綁定／重新綁定改由 `ChannelSelect` 完成，選單只允許 `ChannelType.text`，選到的 `AppCommandChannel` 直接從 Guild cache `resolve()`，不額外 fetch channel；重新整理與解除綁定則是同一面板內的按鈕。這些 component callback 使用 `defer(thinking=False)` 的靜默 acknowledgement，再編輯同一則 ephemeral 面板，不顯示「思考中」。綁定前仍驗證 Bot 在目標文字頻道具有 View Channel、Send Messages，以及 Guild 層級 Create Events、Manage Events；Calendar 看板不依賴 Read Message History，因為已知 `message_id` 的維護直接使用 `PartialMessage`。成功建立新看板並持久化新 binding 後，才 best-effort 刪除舊看板；解除綁定則先移除並持久化 binding，再刪除舊訊息，因此舊公共看板按鈕立即失效，即使訊息刪除失敗也不能再操作。

### 15.2 持久看板

實際看板使用 Components V2 `LayoutView`，四個按鈕使用固定 `custom_id`。跨重啟 dispatch 另由一個不送出的 `View(timeout=None)` 註冊同一組四個按鈕；Bot `setup_hook()` 以 `Client.add_view()` 註冊這個 persistent dispatcher，避免把頂層為 `Container` 的 LayoutView 本身誤當成 persistent registration。每次 callback 都重新核對 interaction 的 Guild／Channel／Message ID 必須完全等於目前 binding，避免 rebind 後的舊看板仍可寫入。

看板以 Python 標準函式庫 `calendar` 顯示目前 UTC+8 月份，活動日用文字標記，下方列最多 8 個近期 Scheduled Events 並使用 Discord timestamp 與 Event URL。正常讀取直接使用 `discord.py` 由 `GUILD_CREATE` 與 Scheduled Event create／update／delete Gateway event 維護的 `Guild.scheduled_events`；`discord.py 2.7.1` 會先更新這份快取再 dispatch event callback，因此看板、瀏覽、編輯列表、AI 查詢、`on_ready` 與午夜刷新都不需要重新呼叫 Scheduled Events List REST。Gateway callback 只重畫看板，已知 `message_id` 時使用 `PartialMessage.edit()`，避免先 GET message；每 Guild 使用一把 async lock 避免同時覆寫。看板訊息被手動刪除且 binding 仍有效時，PATCH 收到 NotFound 後才在同一頻道重建並更新 `message_id`；綁定頻道被刪或 Bot 離開 Guild 時清理 binding。

另有一個不輪詢的午夜生命週期 task：等待下一個 UTC+8 午夜後刷新所有已綁定看板，再重新計算下一個午夜，確保月份自然翻頁。

### 15.3 人工新增與編輯

看板「新增活動」與「編輯活動」每次都重新要求操作使用者具有 Manage Events 或 Administrator。新增 Modal 固定收名稱、`YYYY-MM-DD HH:MM` 開始時間、1 至 10080 分鐘活動長度、地點與選用說明；V1 以固定 UTC+8 解讀輸入，使用 `Guild.create_scheduled_event()` 建立 External Event。

編輯先從目前 Gateway 快取列出 External + Scheduled Events，每頁最多 25 筆；選定後以同一份快取開啟預填 Modal，提交時再用 `Guild.get_scheduled_event()` 檢查目標仍存在且可編輯，不另做 REST GET。`CalendarManager.create_event()`／`edit_event()` 是看板與 AI 確認共用的唯一 mutation 入口，集中做權限、binding、輸入、Event 狀態與 Discord POST／PATCH；mutation 成功後立即回覆使用者，不主動 refresh，後續由 Discord Gateway event 更新 `Guild.scheduled_events` 並重畫看板，避免同一變更重複同步與撞到 Scheduled Events List rate limit。

### 15.4 AI 自然語言

AI 仍只有 `@Bot`／Reply Bot 觸發。已綁定 Calendar 的 Guild 每次 request 建立 `CalendarScope`，包含內部 Guild ID、發問者 ID、當下 Manage Events 能力與 Bot 本地 UTC+8 時間；這些 ID 不序列化給模型。模型可用 `calendar_get_events` 查詢；有管理活動權限時可用 propose tools 產生 `CalendarDraft`。真正 Event ID 只存在當次 `ResearchContext` 的 `event_ref` 對照。

`CalendarDraft` 只存在記憶體，不寫磁碟、不進短期 history 或 Semantic Memory。AI 最終回覆帶 draft 時，Bot 使用原生訊息附上確認／修改／取消按鈕；View 只接受原發問者與原 Guild，10 分鐘後失效。確認時重新檢查目前 binding、使用者 Manage Events／Administrator、Bot 權限與目標 Event；只有通過後才進入共用 mutation method。模型不能以工具成功回覆作為「已建立／已修改」的依據。

### 15.5 V1 邊界

V1 不提供 Event 刪除／取消、Voice／Stage Event 建立或修改、Event 圖片、週期活動、自訂提前提醒、RSVP 管理、Google Calendar／ICS、每 Guild 自訂時區或 Calendar Event database。這些只有出現明確需求並重新確認 `discord.py`／Discord API 能力後才增加。

## 16. 管理控制台

`/控制台`：

- Guild-only；
- Discord default permission 與 runtime 都要求 Administrator；
- ephemeral；
- 只允許原執行者、原 Guild、仍具 Administrator 權限者操作；
- 全部頁面與錯誤只 edit 同一則 interaction response；
- AI 頁只顯示安全的 Model／Effort／health/version；Combo 由 `/v1/models` 確認，公開 metadata 沒有 Effort 時顯示「無法取得」且不列為異常；
- 臨時語音頁重用 `reconcile(prune_absent=False)`；
- Steam 頁重用 `fetch_current_offers()`，不呼叫公開通知流程；
- Server Activity 頁只讀顯示 runtime status、摘要、近期紀錄與篩選結果；
- 停用模組顯示為停用，不列為異常或待設定；
- 不新增 persistent view、資料庫或第二套模組狀態。

## 17. Docker 邊界

### 17.1 Bot

- digest 固定的 Python base image；
- `requirements.txt` 是 Docker、CI、GitHub dependency graph 與 Dependabot 共用的唯一完整 runtime pin set；
- 非 root 使用者；
- read-only root filesystem；
- `/tmp` 使用 16 MiB tmpfs；
- `no-new-privileges`；
- 不公開主機 Port；
- `bot_data` Volume；
- log rotation；
- `restart: unless-stopped`。

### 17.2 9Router

- 官方 9Router `0.5.55` immutable digest；
- `/app/data` 使用 `9router_data` Volume；
- Dashboard 僅綁 `127.0.0.1:20128`；
- Bot 透過 Compose 內部網路使用 `http://9router:20128/v1`；
- `/api/health` healthcheck 必須 HTTP 成功且 JSON `ok=true`；
- `no-new-privileges`、log rotation、停止寬限與 restart policy。

秘密只從 `.env` 注入，不寫入 image 或原始碼。

## 18. 9Router 升級契約

`9router/Dockerfile` 只允許直接繼承官方 immutable digest，不複製或修改 `.next`、壓縮字串、Webpack module 或 9Router 資料庫：

- Alias UI 使用上游原生 Dashboard；
- Chat、Embedding、Models、Search、Fetch 只走公開 `/v1` API；
- `/v1/models/info` 對 Combo 回 404 時，以 `/v1/models` 確認設定 ID，Effort 保持選用資訊；
- 圖片優先接受 Search Provider 原生回傳欄位；沒有時由 Bot 透過公開 `/v1/web/fetch` 對已接受來源做有界 HTML fallback，不修改 9Router bundle。

每次升級只修改官方 digest 與 Compose image tag，然後：

1. 複製目前 `9router_data` 到暫存 Volume；
2. 以候選 image 啟動隔離容器；
3. 驗證 health、version、`/v1/models`、Chat、Embedding、Search 與 Fetch 契約；
4. 執行完整 Bot 測試、兩個 image build 與 9Router smoke test；
5. 證據通過後才另行決定是否部署。

若契約失敗，只能在 `src/ai_client.py` 的 9Router adapter 或 Provider 設定修正；不得重新加入 compiled-bundle patch。

## 19. 模組責任與依賴方向

| 模組 | 主要責任 | 不應承擔 |
|---|---|---|
| `config.py` | 設定解析與驗證 | Discord／HTTP 行為 |
| `preflight.py` | 安全設定摘要 | 啟動服務 |
| `ai_client.py` | 9Router HTTP 協定 | Discord event、工具授權 |
| `agent_tools.py` | 工具 schema、參數驗證、執行 allowlist | Agent 對話流程、Discord 傳送 |
| `chat.py` | history、冷卻、頻道鎖、Agent loop | HTTP payload 細節、Discord UI |
| `discord_images.py` | 附件選取、大小／類型／signature 驗證 | Agent loop |
| `discord_output.py` | 分段與 Discord Components V2 | AI 呼叫 |
| `semantic_memory.py` | SQLite、Embedding queue、搜尋、刪除同步 | Discord trigger |
| `server_activity.py` | 獨立 SQLite、活動佇列／writer、Audit Log 去重、30 天保留、runtime status 與唯讀查詢 | AI／Agent、Chat、9Router、Semantic Memory |
| `calendar_events.py` | Discord Scheduled Events、看板 binding、持久看板、Modal、AI Calendar draft／確認與午夜刷新 | Agent loop、9Router、第二份 Event database |
| `temp_voice.py` | 語音 Parent／Child 與 reconcile | 控制台 UI |
| `steam_free_games.py` | Steam 查詢、去重、通知生命週期 | Agent loop |
| `admin_panel.py` | 管理 UI；Server Activity 只查詢 public APIs，其餘功能只呼叫公開模組動作 | 第二份持久化狀態、直接讀取 Server Activity SQLite |
| `bot.py` | Discord adapter、事件路由、composition root；將活動事件送往 monitor | Provider 協定細節、Server Activity 儲存實作 |

依賴方向以 `bot.py` 組合具體模組為主；功能模組不互相建立實例，也不讀取別人的底線內部欄位。

## 20. 低耦合／高內聚判斷

目前整體分工合理：

- 外部協定已隔離；
- stateful 功能各自擁有狀態與生命週期；
- Agent 工具與 Agent loop 分開；Calendar 的模型工具只讀取／產生草稿，真正 Scheduled Event mutation 集中在 `CalendarManager`；
- Discord 圖片與輸出規則已從 `bot.py` 抽離；
- runtime 設定集中；
- 9Router 只透過公開 `/v1` 契約整合，不再修改上游 bundle；
- 控制台只使用公開摘要與公開動作；
- 沒有需要為了形式再增加 interface、factory、repository 或 service layer。

仍需持續管理的耦合：

1. `src/ai_client.py` 仍依賴 9Router 公開 `/v1` request／response 契約；這是集中且可由測試保護的必要邊界，不再依賴內部 bundle。
2. `bot.py` 是必要的 composition root 與 Discord event adapter，會自然知道所有功能模組；這是可接受的組合耦合，不應再拆成多個無狀態 wrapper。
3. Semantic Memory 的 SQLite full scan 會隨資料量增加；只有量測證明不夠時才更換索引策略。
4. Discord Components V2 仍是 Discord API／`discord.py` 版本耦合面，升級 dependency 時必須跑完整 UI 測試與真人 Discord 驗證。

## 21. 儲存庫內容分工

- `README.md`：新使用者安裝、功能、隱私與日常操作。
- `horo-DCB.md`：本文件；架構原則、資料流與設計邊界。
- `CONTRIBUTING.md`：修改與測試規範。
- `SECURITY.md`：安全通報與部署邊界。
- `.ai-bridge/`：本機代理工作暫存，已由 Git／Docker ignore 排除，不屬於產品。

不要在文件中維護一份聲稱「完整且唯一」的手寫檔案樹；檔案清單應由實際工作區或版本控制系統取得。

## 22. 驗證要求

每次宣告完成前，Server Activity 必須具備 unit、wiring、privacy、queue/load、完整 regression 與 build 證據，並至少重新執行：

```sh
docker compose config --quiet
docker compose build bot 9router
docker compose run --rm --no-deps bot python -m unittest discover -s tests -v
docker compose run --rm --no-deps bot python -m src.preflight
docker compose run --rm --no-deps -e PYTHONPYCACHEPREFIX=/tmp/pycache bot \
  python -m compileall -q src tests
```

Bot image 在建置階段執行 `pip check`，之後移除 runtime `pip`；依賴完整性由 image build 成功證明，不在長駐容器內保留套件管理工具。

測試通過不代表真實 Discord／Provider 已驗證。以下仍需要具備真實憑證與外部服務的整合檢查：

- Bot 能登入 Discord；
- Slash Command 已同步；
- `@Bot`／Reply Bot 實際回覆；
- Components V2 實際畫面與 fallback；
- 9Router Provider／Combo 可用；
- Search → Fetch；
- 圖片輸入與搜尋圖庫；
- Semantic Memory Embedding 與 Discord source verification；
- Calendar `/行事曆` Slash Command 同步、持久看板重啟後仍可操作、同一則訊息更新、Scheduled Event create／edit／delete Gateway 同步、看板誤刪重建，以及 AI 自然語言 draft → 原發問者確認 → 權限重驗 → Discord mutation；
- 臨時語音權限與移動；
- Steam 公開通知。
- Server Activity 的 Audit Log actor／action、普通 Gateway activity、權限與 privileged intent、離線缺口及控制台唯讀查詢。

上述真實 Discord 行為是與單元測試、完整回歸及建置分開的部署 smoke test；沒有對應證據時，必須明確標記為尚未驗證，不得因本文件同步而宣稱已執行。

## 23. 刻意不做

目前不提供：

- 永久保存完整 Discord 對話本文副本；
- DM Semantic Memory；
- 舊 Discord 歷史 backfill；
- 跨 Channel／Guild／User-wide Semantic Memory 搜尋；
- 外部 Vector DB、Redis、PostgreSQL 或工作佇列服務；
- PDF／文件／音訊／影片理解與長期記憶；
- 語音播放、錄音或辨識；
- 任意 Shell、檔案系統、HTTP、Discord 管理工具；Calendar 只允許本文件定義、需人工確認的 Scheduled Event create／edit；
- Calendar Event 本地資料庫、Event 刪除／取消、Voice／Stage create/edit、Event 圖片、週期活動、自訂提前提醒、Google Calendar／ICS、RSVP 管理與每 Guild 自訂時區；
- 多 Agent 或 MCP；
- Semantic Memory 管理控制台；
- 自動 Embedding model migration；
- 多個 9Router instance；
- 自訂 9Router SDK。

只有出現可驗證需求或量測瓶頸時才增加。
