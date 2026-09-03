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
3. DM 直接拒絕；Guild、parent Channel 與 User 必須全部命中 allowlist。
4. Bot 驗證文字與附件，建立 conversation key。
5. Bot 透過固定 http://codex:8765 與 Bearer token 呼叫 bridge。
6. Sidecar 驗證 token、JSON schema、文字與 data URL 上限。
7. 首次對話建立 Codex thread；後續依 mapping resume。
8. 回覆經既有 Discord splitting／TextDisplay 輸出。

一般 Channel 依 User 分開 thread；Discord Thread 中所有 allowlisted 使用者共用該 Thread 的 Codex thread。旁觀訊息不會同步。

## 3. Trust boundaries

Bot 看不到 CODEX_HOME 或 codex_data。Sidecar 看不到 DISCORD_TOKEN、bot_data 或非 AI 功能設定。Bridge 使用 64 字元小寫十六進位 shared secret，Authorization 比對採 constant time。

Bridge log 不記錄 prompt、圖片、Bearer token、OAuth email 或完整 Codex exception。回 Discord 的錯誤為固定繁體中文文字，不包含 RPC payload 或 OAuth 狀態。

使用者文字、圖片與 Web Search 結果都視為不可信資料。永久 allowlist 是必要安全邊界，不得改成公開 Bot。

## 4. Bridge contract

GET /healthz 不需 token，只回：

~~~json
{"status":"ready"}
~~~

未登入或 runtime 不可用時回 503 與 not_ready。

GET /v1/status 需要 Bearer token，回傳 available、authenticated、plan、sdk_version、runtime_version、web_search 與 thread_count，不回傳 email 或 OAuth token。

POST /v1/chat：

~~~json
{
  "conversation_key": "guild:1:channel:2:user:3",
  "display_name": "Steven",
  "text": "今天有什麼重要新聞？",
  "images": []
}
~~~

成功只回 reply。錯誤只使用 invalid_request、unauthorized、usage_limit_or_unavailable、auth_required、unavailable 或 timeout。

POST /v1/archive 接受 guild_id 與選用的 channel_id，移除符合 mapping，並透過公開 thread_archive best-effort archive。

## 5. Codex runtime

依賴固定：

- openai-codex==0.147.0
- openai-codex-cli-bin==0.147.0

不指定 model 或 reasoning effort，讓 ChatGPT 帳號使用可用預設。CodexConfig 固定 ChatGPT login、file credential store、live Web Search、禁止 startup update，並停用 apps、goals、hooks、memories、multi-agent、remote plugin、shell snapshot、shell tool 與 unified exec。

每個 thread 使用 Sandbox.read_only、ApprovalMode.deny_all、空白 /app/codex-workspace 與 shell_environment_policy.inherit=none。沒有 MCP server、filesystem write、connector、subagent 或 write action。

Device login 由 SDK 的 login_chatgpt_device_code 執行；horo-DCB 不解析、不保存也不刷新 OAuth token。

## 6. Persistent data

bot_data 保留 Calendar、Steam、temporary voice 與 Server Activity 狀態。既有 Semantic Memory SQLite 不做 migration，新 image 不會開啟或修改它。

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

mapping 使用 temporary file、chmod 0600 與 os.replace。格式或 version 錯誤時 startup fail closed，不覆寫損壞檔案。

刪除 Channel／Thread 或離開 Guild 時只移除對應 mapping 並 archive thread。清除整個 codex_data 才會同時移除 OAuth 與所有本機 thread state。

## 7. Input bounds

- conversation key 必須符合既定 Guild／Channel／Thread 格式。
- display name 正規化並限制 80 字元。
- text 最多 4,000 字元，空文字必須至少有一張圖。
- 圖片僅 JPEG、PNG、WebP；最多 4 張、單張 8 MiB、總計 16 MiB。
- HTTP body 上限 24 MiB。
- 同一 conversation 序列化；不同 conversation 可並行。
- turn timeout 後 interrupt，不 retry。

## 8. Module ownership

| Module | Responsibility |
| --- | --- |
| src/config.py | 唯一 Bot environment parser 與 fail-closed validation |
| src/codex_bridge_client.py | allowlist、conversation key、cooldown、bridge HTTP |
| src/codex_bridge.py | SDK lifecycle、thread store、HTTP boundary、error normalization |
| src/bot.py | Discord composition root 與 direct-interaction routing |
| src/admin_panel.py | 唯讀 Codex／非 AI runtime status |
| src/calendar_events.py | Calendar 看板、Modal、人工建立／修改 |
| src/steam_free_games.py | Steam 排程與通知 |
| src/temp_voice.py | 臨時語音 |
| src/server_activity.py | 獨立 activity state |
| src/discord_images.py | Discord 圖片驗證與 data URL |
| src/discord_output.py | Markdown splitting 與 Components V2 |

已刪除 AIClient、AgentTools、ChatManager、SemanticMemory 與 9Router image。Calendar／Steam 不暴露為自然語言 tool。

## 9. Verification gates

自動驗收包含 config、allowlist、bridge authentication／validation／thread persistence／concurrency／archive／timeout、Bot direct interaction、圖片、Discord output、admin panel，以及所有非 AI 回歸。

Build gate 必須通過 Compose config、shared image build、完整 unittest、compileall、SDK/runtime 0.147.0、runtime 無 pip，以及 container Volume／secret／port 隔離。

真人 gate 必須用目標 ChatGPT 帳號驗證 account、普通 turn、resume、app-server restart 後 resume、live Web Search event／來源連結，以及 JPEG／PNG／WebP。任一失敗即停止，不改用 API key，也不恢復自動 fallback。

## 10. Deployment

horo-server 負責 source、測試與 candidate build。horo-laptop 是 image-only 正式環境，不保留 Git checkout。

切換順序：

1. 建置、測試並以完整 OCI archive 搬移 image。
2. 比對兩端 image ID、architecture 與 RootFS layers。
3. 在 laptop 的 codex_data 完成 device login。
4. 設定 mode 0600 的 .env 與精確 allowlist。
5. 停舊 Bot，啟動 codex 並等待 healthy，再啟動新 Bot。
6. 完成 owner-only chat、resume、Web Search、三種圖片、控制台與非 AI smoke。
7. 七天內保留舊 compose、images 與 Volumes。

沒有資料 migration。回滾只需停止新服務、還原舊 compose／images 並驗證舊服務；codex_data 保留供診斷。七天後清理舊 9Router 與 semantic_memory.sqlite3 必須再次人工確認。
