# horo-DCB

horo-DCB 是 Discord Bot。AI 對話由獨立 Codex sidecar 經官方 openai-codex Python SDK 執行，使用 ChatGPT device login；Bot 不接觸 OAuth access token 或 refresh token。

## 架構

~~~
Discord member with allowlisted role
        |
        v
Bot container -- authenticated private HTTP --> Codex sidecar
                                                   |
                                                   v
                              official openai-codex SDK / app-server
                              ChatGPT OAuth, persistent threads,
                              image input, live Web Search
~~~

Bot 與 sidecar 使用同一個 immutable image、不同 command 和不同 Volume：

- bot 只掛載 bot_data，持有 Discord Token 與非 AI 功能狀態。
- codex 只掛載 codex_data，持有 OAuth cache 與 Codex thread mapping。
- Bridge port 8765 只在 Compose network 內存在，不發布到 host。
- 兩端以 CODEX_BRIDGE_TOKEN 的 Bearer token 驗證。

沒有 9Router、MCP、自製 OAuth、API key fallback、Semantic Memory 或自製 Web Search／Fetch。

## 使用者功能

保留：

- Bot mention 與 reply 的繁體中文文字聊天。
- JPEG、PNG、WebP 圖片輸入。
- Codex live Web Search 與 Markdown 來源連結。
- Discord Calendar 看板、Modal、人工建立／修改活動。
- Steam 限時免費排程、通知與控制台。
- 臨時語音、Server Activity、Discord Components V2 與長訊息分段。

暫停：

- 自然語言 Calendar 查詢或草稿。
- 自然語言 Steam 查詢。
- Semantic Memory 與頻道旁聽 history。
- Web 搜尋圖片 Gallery。

Codex 失敗會顯示固定安全訊息，不會 fallback 到其他模型服務。

## 安裝

需求：

- Docker Engine 與 Docker Compose v2。
- Discord Application、Bot Token 與 Message Content Intent。
- 可使用 Codex 的 ChatGPT 帳號；實際方案額度、device login、持久 resume、live search 與圖片能力必須在部署前真人驗證。

以下先在隔離 Linux 來源工作區產生範例設定。實際憑證只放在目標部署主機；GitHub Actions 使用假設定建置與測試，不做 device login 或連線真人服務：

~~~sh
sh scripts/setup.sh
~~~

接著：

1. 在目標部署主機的 .env 填入 DISCORD_TOKEN；不要把真實憑證送進測試 runner。
2. 在獨立 Linux／Docker 測試沙箱或 CI 建置共用 image：

~~~sh
docker compose build bot
~~~

3. 將通過 CI 的 image 交給目標主機的 image-only Compose。只在首次啟用或登入失效時，於目標主機完成 device login：

~~~sh
docker compose run --rm --no-deps codex python -m src.codex_bridge login
~~~

4. 在 Discord 開發者模式複製允許的 Guild ID，填入：

~~~dotenv
CODEX_ENABLED=1
CODEX_ALLOWED_GUILD_ID=123
~~~

5. 驗證、啟動，再由伺服器管理員輸入 `/控制台`，到「AI 助手」選擇 1–25 個一般文字頻道與身分組：

~~~sh
docker compose config --quiet
docker compose up -d
~~~

`CODEX_ALLOWED_CHANNEL_ID` 與 `CODEX_ALLOWED_USER_IDS` 是相容種子。v1／v2 狀態使用 legacy 使用者模式；保存角色後使用 v3 角色模式，空角色集合也不會回退至舊使用者。狀態損壞時必須先重選頻道、再選角色才重新開放。

角色變更會先暫停接收、取消並等待舊工作，再移除對話 mapping、best-effort 封存並保存新角色。整個變更由共用鎖序列化；失敗不套用新角色。移除頻道後，即使封存失敗，被移除的頻道仍不可使用；單純新增頻道保留既有對話。

一般頻道必須同時符合 Guild、Channel 與任一 Role allowlist。Discord Thread 使用 parent channel 驗證並由合資格成員共用 Codex 對話；DM 與其他 Guild／Channel／Role 都不呼叫 Codex。

## 設定

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| DISCORD_TOKEN | 必填 | Discord Bot Token。 |
| CODEX_ENABLED | 0 | 是否開放 Codex 對話。 |
| CODEX_ALLOWED_GUILD_ID | 空 | 唯一允許的 Guild snowflake。 |
| CODEX_ALLOWED_CHANNEL_ID | 空 | 可選的第一個頻道預設；之後由 `/控制台` 管理 1–25 個頻道。 |
| CODEX_ALLOWED_USER_IDS | 空 | 可選的 legacy 使用者白名單；第一次保存角色後不再授權。 |
| CODEX_BRIDGE_TOKEN | setup 產生 | 64 字元小寫十六進位 bridge secret。 |
| AI_TEXT_DISPLAY_ENABLED | 1 | Discord Components V2 文字輸出。 |
| TEMP_VOICE_ENABLED | 0 | 臨時語音。 |
| STEAM_FREE_GAMES_ENABLED | 0 | Steam 限免通知。 |
| SERVER_ACTIVITY_ENABLED | 0 | Server Activity 持久記錄。 |

CODEX_ENABLED=0 時 allowlist 可留空，但 Codex sidecar 固定存在，因此 CODEX_BRIDGE_TOKEN 必須始終有效；設為 1 時 Guild 必須有效，頻道與角色可在啟動後由控制台設定。尚未完成任何有效授權組合時 AI 一律拒絕請求。Bridge URL 固定為 http://codex:8765，不是部署選項。

## 對話與圖片

一般頻道 mapping：

~~~text
guild:<guild_id>:channel:<channel_id>:user:<user_id>
~~~

Thread mapping：

~~~text
guild:<guild_id>:thread:<thread_id>
~~~

只有 mention Bot 或 reply Bot 的直接互動會寫入對應 Codex thread。Thread ID 保存於 codex_data 的 horo_threads.json，採原子寫入與 mode 0600。Bot 或 sidecar restart 後可 resume；不搬移舊 Semantic Memory。

圖片限制：

- JPEG、PNG 或 WebP。
- 最多 4 張。
- 單張最多 8 MiB。
- 總計最多 16 MiB。

## Codex runtime 限制

目前固定 openai-codex 0.147.0 與 openai-codex-cli-bin 0.147.0。程式沿用 Codex 設定的 model 與 reasoning effort；horo-laptop 目前保留 `gpt-5.6-luna`／`medium`。Sidecar 使用：

- live Web Search。
- read-only sandbox。
- deny-all approval。
- 空白 /app/codex-workspace。
- 不繼承 shell environment。
- 關閉 apps、goals、hooks、memories、multi-agent、remote plugin、shell 與 unified exec。

Codex local memories 不啟用。對話內容、圖片與搜尋請求會送往 OpenAI；不要在 Discord 輸入秘密。

## 請求限制與故障處理

- 同時處理 2 個 AI 工作，最多 4 個等待工作；同一對話最多額外等待 1 則。
- 排隊最多 30 秒，圖片下載最多 15 秒，SDK 建立／恢復／執行合計最多 120 秒。
- Bot 接受工作後總期限為 150 秒，包含排隊、圖片與回覆送出；各階段上限不相加。
- 已處理工作的成功或錯誤回覆均保持同一對話順序；錯誤通知與取消清理另有最多 5 秒界線。忙碌／排隊到期通知可立即回覆，不重新排隊。
- 權限或設定變更後，舊工作失效；送出模型請求與 Discord 分段前重新檢查目前成員角色。
- 生成逾時中斷該輪；無回應的 RPC、transport 結束或中斷失敗會回收 sidecar。登入／額度錯誤不引發重啟循環，也不自動重送問題。
- 對話映射寫入失敗後停止後續聊天寫入，保留原檔；修復儲存問題後重新啟動以載入有效狀態。

## 維運

安全狀態：

~~~sh
docker compose ps
docker compose logs codex
docker compose run --rm -T --no-deps bot python -m src.preflight
~~~

preflight 不輸出 Discord Token、bridge token、OAuth email 或 allowlist ID。`/控制台` 先回應 Discord，再用獨立 3 秒期限查詢狀態，Codex 故障時仍可操作其他模組。

健康探測表示 RPC、登入與映射儲存就緒；實際聊天失敗另以安全錯誤碼顯示。控制台分開呈現 Bot 與 bridge 的工作數，避免重複計算。日誌只記錄階段耗時、數量與固定結果；成功健康探測去除重複紀錄，失敗與狀態轉換保留。

清除 codex_data 會同時刪除 OAuth cache 與所有本機 thread mapping，之後必須重新 device login。請先停止服務、確認實際 Volume 名稱，再由管理員人工刪除；不要把 Volume 內容貼到聊天、issue 或 log。

Discord channel／thread 被刪除或 Bot 離開 Guild 時，mapping 會移除並 best-effort archive 對應 Codex thread。實體資料仍由 codex_data 的人工生命週期管理。

## 測試

以下指令只在獨立 Linux／Docker 沙箱執行。horo-PC、horo-server 與正式 horo-laptop 不執行專案測試或安裝測試套件；目前使用 GitHub Actions 的一次性 Ubuntu runner。

~~~sh
docker compose config --quiet
docker compose build bot
docker compose run --rm -T --no-deps bot python -m unittest discover -s tests -v
docker compose run --rm --no-deps -e PYTHONPYCACHEPREFIX=/tmp/pycache bot python -m compileall -q src tests
~~~

Runtime image 建置時執行 pip check，完成後移除 pip。CI 另外驗證 SDK／runtime 版本與兩個容器的 Volume、secret、port 隔離。

## 部署與回滾

正式 horo-laptop 保持 image-only，只保存 .env、compose.yaml、共享 runtime image、bot_data 與 codex_data。原始碼在 horo-server 的隔離 worktree 修改，GitHub Actions 建置、測試並匯出同一個通過驗證的 image。

每個 push 候選 artifact 包含完整 `docker save` 壓縮檔、SHA-256、`image-identity.json`（篩選後的映像身分資料）與來源 commit。Image 的 OCI labels 記錄 source／revision。部署下載該 artifact，核對 commit、archive hash、image ID、architecture 與 RootFS layers，再先啟動 codex 至 healthy、最後啟動 bot。

切換前保留舊 compose、舊 image 與必要狀態回復點至少七天，自實際切換起算。既有 mapping v1 與 access v2／v3 格式保留；回滾優先還原 image／compose，不整批覆蓋 bot_data。

舊版可能對空 v3 角色回退至 legacy 使用者。回滾前若授權檔缺失、損壞或角色為空，先維持 AI 停用，保留資料並修復有效授權，再重新開放。刪除授權檔不是修復方式。七天後的舊 9Router／Semantic Memory 清理仍須人工確認。
