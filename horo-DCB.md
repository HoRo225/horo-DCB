# horo-DCB 技術架構

## 1. Runtime topology

Compose 只包含 bot 與 codex。兩者共用 horo-dcb:local image，但權限與資料完全分離。

| Service | Command | Secrets | Volume |
| --- | --- | --- | --- |
| bot | python -m src.bot | Discord Token、bridge token、allowlist | bot_data:/app/data |
| codex | python -m src.codex_bridge serve | bridge token、Codex OAuth cache | codex_data:/app/codex |

codex 的 8765 port 不發布到 host。兩個 container 都以 UID/GID 10001、read-only root filesystem 與 no-new-privileges 執行，只有專屬 Volume 與小型 tmpfs 可寫。

## 2. Request flow

1. Discord event 進入 HoroBot。
2. 只接受直接 mention Bot 或 reply Bot。
3. DM 直接拒絕；Guild、parent Channel 與至少一個 Member Role 必須命中 allowlist。
4. Bot 驗證文字與附件，建立 conversation key。
5. Bot 透過固定 http://codex:8765 與 Bearer token 呼叫 bridge。
6. Sidecar 驗證 token、JSON schema、文字與 data URL 上限。
7. 首次對話建立 Codex thread；後續依 mapping resume。
8. 回覆經既有 Discord splitting／TextDisplay 輸出。

白名單 Guild 由環境固定；1–25 個頻道與角色由 `/控制台` 保存於 `bot_data/codex_access.json`。version 1／2 可直接讀取並暫用 legacy User IDs，第一次保存角色後寫成 version 3 並永久只依角色授權。v3 空角色仍屬角色模式並拒絕全部；正常 legacy 模式保存 v2。角色變更先暫停、提升設定世代並取消舊工作，再移除 mapping、best-effort 封存，成功保存後才套用。

一般 Channel 依 User 分開 thread；Discord Thread 中所有合資格角色成員共用該 Thread 的 Codex thread。旁觀訊息不會同步。

## 3. Trust boundaries

Bot 看不到 CODEX_HOME 或 codex_data。Sidecar 看不到 DISCORD_TOKEN、bot_data 或非 AI 功能設定。Bridge 使用 64 字元小寫十六進位 shared secret，Authorization 比對採 constant time。

Bridge log 不記錄 prompt、圖片、Bearer token、OAuth email 或完整 Codex exception。回 Discord 的錯誤為固定繁體中文文字，不包含 RPC payload 或 OAuth 狀態。

使用者文字、圖片與 Web Search 結果都視為不可信資料。永久 allowlist 是必要安全邊界，不得改成公開 Bot。

頻道／角色狀態檔採 versioned JSON、mode 0600 與原子替換；只有真正不存在的檔案能採用 legacy 啟動種子，其他讀取錯誤 fail closed。管理員修復時必須依序設定頻道與角色；僅設定頻道會保存 v3 空角色，重啟後仍拒絕舊使用者。

## 4. Bridge contract

GET /healthz 不需 token，只回：

~~~json
{"status":"ready"}
~~~

未登入或 runtime 不可用時回 503 與 not_ready。

GET /v1/status 需要 Bearer token，回傳 available、authenticated、plan、sdk_version、runtime_version、web_search、thread_count，以及 bridge 自有的 active_requests、queued_requests、last_error。Client 對缺少的新欄位採安全預設，控制台另外呈現 Bot 工作數；兩層數值不相加。不回傳 email、OAuth token 或對話內容。

POST /v1/chat：

~~~json
{
  "conversation_key": "guild:1:channel:2:user:3",
  "display_name": "Steven",
  "text": "今天有什麼重要新聞？",
  "images": []
}
~~~

成功只回 reply。錯誤只使用 invalid_request、unauthorized、usage_limit_or_unavailable、auth_required、unavailable、timeout 或 busy；busy 回 HTTP 429。Authorization 非 ASCII 也回固定 401。

POST /v1/archive 接受 guild_id 與選用的 channel_id，移除符合 mapping，並透過公開 thread_archive best-effort archive。

## 5. Codex runtime

依賴固定：

- openai-codex==0.147.0
- openai-codex-cli-bin==0.147.0

程式沿用 Codex config 的 model 與 reasoning effort；horo-laptop 目前設定為 gpt-5.6-luna／medium。CodexConfig 固定 ChatGPT login、file credential store、live Web Search、禁止 startup update，並停用 apps、goals、hooks、memories、multi-agent、remote plugin、shell snapshot、shell tool 與 unified exec。

每個 thread 使用 Sandbox.read_only、ApprovalMode.deny_all、空白 /app/codex-workspace 與 shell_environment_policy.inherit=none。沒有 MCP server、filesystem write、connector、subagent 或 write action。

Device login 由 SDK 的 login_chatgpt_device_code 執行；horo-DCB 不解析、不保存也不刷新 OAuth token。

## 6. Persistent data

bot_data 只保留 Codex Channel／Role access、Calendar、Steam 與 temporary voice 狀態。Bot 不使用 app-owned SQLite，也不保存 Guild 活動紀錄。

codex_data 包含 Codex OAuth cache 與 horo_threads.json：

~~~json
{
  "version": 1,
  "threads": {
    "guild:1:channel:2:user:3": {
      "thread_id": "thr_example",
      "updated_at": 1788451200
    }
  }
}
~~~

mapping 使用候選 dict、temporary file、chmod 0600 與 os.replace；持久化成功後才更新記憶體。格式或 version 錯誤時 startup fail closed，不覆寫損壞檔案。寫入失敗鎖定儲存不可用，後續與等待中的聊天不得再開 SDK 工作或寫檔；修復後由新程序載入原本有效 mapping。

刪除 Channel／Thread 或離開 Guild 時只移除對應 mapping 並 archive thread。清除整個 codex_data 才會同時移除 OAuth 與所有本機 thread state。

## 7. Input bounds

- conversation key 必須符合既定 Guild／Channel／Thread 格式。
- display name 正規化並限制 80 字元。
- text 最多 4,000 字元，空文字必須至少有一張圖。
- 圖片僅 JPEG、PNG、WebP；最多 4 張、單張 8 MiB、總計 16 MiB。
- HTTP body 上限 24 MiB。
- 每程序最多 2 個 active／4 個 waiting；同 key 最多額外等待 1 個，同 key 持有權涵蓋 Discord 成功與錯誤輸出。
- queue 30 秒、images 15 秒、整個 SDK start／resume／turn／run 120 秒；Bot 總工作期限 150 秒，取消或錯誤通知最多另加 5 秒。
- 工作進入 SDK 前登記；archive 先封鎖範圍、取消並等待工作，再移除 matching mapping，避免在途 start 重新建立資料。
- SDK 初始化 30 秒、status RPC 2 秒；Bot status HTTP 3 秒、chat HTTP 125 秒。
- 有 handle 的生成逾時會 bounded interrupt；無回應的 RPC、TransportClosedError 或中斷失敗回收程序。登入／額度錯誤只回報；不 retry 原始問題。
- 關閉、HTTP 斷線、角色撤銷與設定世代改變均會取消工作，重複取消不打斷清理。

## 8. Module ownership

| Module | Responsibility |
| --- | --- |
| src/config.py | 唯一 Bot environment parser 與 fail-closed validation |
| src/codex_bridge_client.py | 明確授權模式、共用 admission／取消、設定世代、cooldown 與 bridge HTTP |
| src/codex_bridge.py | SDK lifecycle、thread store、HTTP boundary、error normalization |
| src/bot.py | Discord composition root 與 direct-interaction routing |
| src/admin_panel.py | Codex／非 AI 狀態與受共用鎖保護的頻道／角色設定 |
| src/calendar_events.py | Calendar 看板、Modal、人工建立／修改 |
| src/steam_free_games.py | Steam 排程與通知 |
| src/temp_voice.py | 臨時語音 |
| src/discord_images.py | Discord 圖片驗證與 data URL |
| src/discord_output.py | Markdown splitting 與 Components V2 |

已刪除 AIClient、AgentTools、ChatManager、SemanticMemory、Server Activity 與 9Router。Members intent 僅供白名單角色即時撤權，不保存成員事件。Calendar／Steam 不暴露為自然語言 tool。

## 9. Verification gates

自動驗收包含 config、allowlist、bridge authentication／validation／thread persistence／concurrency／archive／timeout、Bot direct interaction、圖片、Discord output、admin panel，以及所有非 AI 回歸。

Build gate 必須通過 Compose config、shared image build、完整 unittest、compileall、SDK/runtime 0.147.0、runtime 無 pip，以及 container Volume／secret／port 隔離。

真人 gate 必須用目標 ChatGPT 帳號驗證 account、普通 turn、resume、app-server restart 後 resume、live Web Search event／來源連結，以及 JPEG／PNG／WebP。任一失敗即停止，不改用 API key，也不恢復自動 fallback。

## 10. Deployment

horo-server 保存隔離原始碼 worktree；建置與測試使用 GitHub Actions 一次性 Linux／Docker runner，通過後匯出同一 image artifact。horo-PC／horo-server／正式 horo-laptop 不執行專案測試。horo-laptop 是 image-only 正式環境，不保留 Git checkout。

切換順序：

1. 從通過完整 CI 的 push commit 下載候選 artifact，驗證 archive SHA-256 與 OCI source／revision，再以完整 OCI archive 搬移 image。
2. 比對兩端 image ID、architecture 與 RootFS layers。
3. 保留 laptop 的 codex_data；只在首次啟用或登入失效時完成 device login。
4. 設定 mode 0600 的 .env 與精確 allowlist。
5. 停舊 Bot，啟動 codex 並等待 healthy，再啟動新 Bot。
6. 完成 owner-only chat、resume、Web Search、三種圖片、控制台與非 AI smoke。
7. 七天內保留前一個 Codex-era compose／image 與現行 Volumes；不保留 pre-Codex／9Router／app-owned SQLite 回復點。

保留既有 JSON 版本。回滾停止新服務、恢復 Codex-era compose／image，保留 codex_data 與其他模組的最新資料。若授權檔缺失、損壞或為 v3 空角色，舊版啟動前維持 AI 停用，避免 legacy 回退；不用整份 volume 回復來處理程式回滾。pre-Codex／9Router 與舊 Semantic Memory 不再提供回滾。
