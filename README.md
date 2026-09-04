# horo-DCB

horo-DCB 是 Discord Bot。AI 對話由獨立 Codex sidecar 經官方 openai-codex Python SDK 執行，使用 ChatGPT device login；Bot 不接觸 OAuth access token 或 refresh token。

## 架構

~~~
Allowlisted Discord user
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

在 source host：

~~~sh
sh scripts/setup.sh
~~~

接著：

1. 在 .env 填入 DISCORD_TOKEN。
2. 建置共用 image：

~~~sh
docker compose build bot
~~~

3. 完成 device login：

~~~sh
docker compose run --rm --no-deps codex python -m src.codex_bridge login
~~~

4. 在 Discord 開發者模式複製允許的 Guild 與 User ID，填入：

~~~dotenv
CODEX_ENABLED=1
CODEX_ALLOWED_GUILD_ID=123
CODEX_ALLOWED_USER_IDS=789,101112
~~~

5. 驗證、啟動，再由伺服器管理員輸入 `/控制台`，到「AI 助手」選擇一般文字頻道：

~~~sh
sh scripts/check-env.sh
docker compose up -d
~~~

`CODEX_ALLOWED_CHANNEL_ID` 可保留作首次升級預設值；控制台選擇後會以 mode 0600 原子保存至 `bot_data`，並優先於環境變數。切換至不同頻道時會封存該 Guild 的既有 Codex 對話；封存失敗不會回退新頻道。

一般頻道必須同時符合 Guild、Channel 與 User allowlist。Discord Thread 使用 parent channel 驗證；DM、其他 Guild／Channel／User 都不呼叫 Codex。

## 設定

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| DISCORD_TOKEN | 必填 | Discord Bot Token。 |
| CODEX_ENABLED | 0 | 是否開放 Codex 對話。 |
| CODEX_ALLOWED_GUILD_ID | 空 | 唯一允許的 Guild snowflake。 |
| CODEX_ALLOWED_CHANNEL_ID | 空 | 可選的首次頻道預設；之後由 `/控制台` 管理。 |
| CODEX_ALLOWED_USER_IDS | 空 | 逗號分隔、不得重複的正整數 snowflake。 |
| CODEX_BRIDGE_TOKEN | setup 產生 | 64 字元小寫十六進位 bridge secret。 |
| AI_TEXT_DISPLAY_ENABLED | 1 | Discord Components V2 文字輸出。 |
| TEMP_VOICE_ENABLED | 0 | 臨時語音。 |
| STEAM_FREE_GAMES_ENABLED | 0 | Steam 限免通知。 |
| SERVER_ACTIVITY_ENABLED | 0 | Server Activity 持久記錄。 |

CODEX_ENABLED=0 時 allowlist 可留空，但 Codex sidecar 固定存在，因此 CODEX_BRIDGE_TOKEN 必須始終有效；設為 1 時 Guild 與 User allowlist 必須有效，頻道可在啟動後由控制台設定。尚未選擇頻道時 AI 一律拒絕請求。Bridge URL 固定為 http://codex:8765，不是部署選項。

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

目前固定 openai-codex 0.147.0 與 openai-codex-cli-bin 0.147.0，不指定 model 或 reasoning effort。Sidecar 使用：

- live Web Search。
- read-only sandbox。
- deny-all approval。
- 空白 /app/codex-workspace。
- 不繼承 shell environment。
- 關閉 apps、goals、hooks、memories、multi-agent、remote plugin、shell 與 unified exec。

Codex local memories 不啟用。對話內容、圖片與搜尋請求會送往 OpenAI；不要在 Discord 輸入秘密。

## 維運

安全狀態：

~~~sh
docker compose ps
docker compose logs codex
docker compose run --rm -T --no-deps bot python -m src.preflight
~~~

preflight 不輸出 Discord Token、bridge token、OAuth email 或 allowlist ID。

清除 codex_data 會同時刪除 OAuth cache 與所有本機 thread mapping，之後必須重新 device login。請先停止服務、確認實際 Volume 名稱，再由管理員人工刪除；不要把 Volume 內容貼到聊天、issue 或 log。

Discord channel／thread 被刪除或 Bot 離開 Guild 時，mapping 會移除並 best-effort archive 對應 Codex thread。實體資料仍由 codex_data 的人工生命週期管理。

## 測試

~~~sh
docker compose config --quiet
docker compose build bot
docker compose run --rm -T --no-deps bot python -m unittest discover -s tests -v
docker compose run --rm --no-deps -e PYTHONPYCACHEPREFIX=/tmp/pycache bot python -m compileall -q src tests
~~~

Runtime image 建置時執行 pip check，完成後移除 pip。CI 另外驗證 SDK／runtime 版本與兩個容器的 Volume、secret、port 隔離。

## 部署與回滾

正式 horo-laptop 保持 image-only，只保存 .env、compose.yaml、共享 runtime image、bot_data 與 codex_data。候選 image 應以完整 docker save archive 搬移並比對 image ID、architecture 與 RootFS layers。

切換前保留舊 compose、舊 images、bot_data 與舊 service Volume 七天。新版本沒有資料 schema migration；失敗時停止 Bot／Codex，還原舊 compose 與 images 即可。七天後的舊 9Router／Semantic Memory 清理必須再次人工確認。
