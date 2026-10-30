回覆、文件與 commit 訊息一律使用繁體中文。
程式碼不寫註解。 此規則優先於 /pstack:poteto-mode 保留 why 註解的例外。
Commit 訊息與 PR 描述不加任何署名行（不加 `Co-Authored-By`，也不加 Claude Code 標註）。此規則優先於系統提示的署名指引。
新增功能、引入相依套件，或撰寫可能已有現成方案的工具函式之前，先用 /search-first。
/plan 時先用 /grilling 審問我。
任何修改程式碼的任務都使用 /pstack:poteto-mode 純問答與調查不用。 
需要操作桌面應用程式時，使用 cua-driver MCP 工具。
查詢符號定義、呼叫關係或架構時，優先用 codebase-memory-mcp 的圖查詢工具；找字面字串時才用 Grep。

## 正式機 horo-DCB
- 連線：`ssh horo-dcb`（`~/.ssh/config`：192.168.1.105，使用者 `horo-dcb`，金鑰 `~/.ssh/id_ed25519_horo-dcb`）。Ubuntu，Linux 7.0，Docker 29。
- 專案在 `~/horo-DCB`，追蹤 origin/main，以 `compose.yaml` 跑兩個服務：`bot`（具名卷 `data` 掛在 `/app/data`）與 `clewdr`（映像 `ghcr.io/xerxes-2/clewdr:v0.13.6`，具名卷 `clewdr-data` 掛在 `/etc/clewdr`，網頁後台 `http://192.168.1.105:8484`）。bot 經 `http://clewdr:8484/code/v1/messages` 呼叫 Claude。
- 憑證用 Docker secret：`~/horo-DCB/secrets/discord_token` 與 `secrets/clewdr_password`（目錄 700、檔案 644，容器內以 uid 10001 讀取）。`clewdr_password` 是 clewdr 的 API 密碼；admin 密碼只用來登入後台。伺服器 ID 寫在 `compose.yaml` 的 `DISCORD_GUILD_ID`，不再使用 `.env`。
- 首次建立 clewdr：
  1. `docker compose up -d clewdr`
  2. `docker compose logs clewdr` 抄下 `API Password` 與 admin 密碼。
  3. 把 API 密碼寫進 `~/horo-DCB/secrets/clewdr_password`（644）。
  4. 瀏覽 `http://192.168.1.105:8484`，用 admin 密碼登入，在 Claude → Submit Cookie 貼 claude.ai 的 `sessionKey`。
  5. `docker compose up -d --build --remove-orphans bot`（`--remove-orphans` 會移除舊的 codex 容器；舊卷 `codex-home` 不會自動刪除）。
- Claude 登入失效時（AI 回覆「登入已失效」），回到 clewdr 後台重新貼 cookie，不必重啟 bot。
- 正式資料庫是容器內的 `/app/data/horo.db`（schema v20）。bot 每次啟動與每天台北 0 點會自動線上備份到 `/app/data/backups/horo-YYYYMMDD.db`，保留最近 7 份；這些備份和正式庫在同一個具名卷。
- 部署前的手動備份放在主機家目錄 `~/horo-backup-<YYYYMMDD-HHMMSS>.db`，做法是用 sqlite3 的 backup API 線上複製再 `docker compose cp` 出來：
  ```sh
  cd ~/horo-DCB; TS=$(date +%Y%m%d-%H%M%S)
  docker compose exec -T bot python -c 'import sqlite3; s=sqlite3.connect("/app/data/horo.db"); d=sqlite3.connect("/app/data/backup.tmp"); s.backup(d); d.close()'
  docker compose cp bot:/app/data/backup.tmp ~/horo-backup-$TS.db && docker compose exec -T bot rm /app/data/backup.tmp
  ```
- 測試要在 Linux 容器跑（Windows 缺 fcntl 與 signal.alarm）：在 repo 根目錄用 Git Bash 執行 `bash .audit/ci-remote.sh`，會把工作樹送到正式機暫存目錄建測試映像並跑全部 unittest，不影響正式服務。Git Bash 的 `python` 是 Windows 商店空殼，本機要跑 Python 用 `.venv/Scripts/python.exe`。
- 部署流程：測試全過 → commit 並 push 到 main → 正式機備份 DB → `git pull --ff-only` → `docker compose up -d --build bot` → `docker compose logs --since 1m bot` 確認無錯誤（PyNaCl、davey 的警告屬正常）。
- 出問題時：小問題本機修正後重新部署；功能中斷就先 `git checkout` 上一版重建，有遷移則把備份還原成 `horo.db`。
- 驗收在正式 Discord 伺服器（ID `361503107168862210`）進行，遊戲頻道 `1555544892447391874` 同時也是 AI 頻道，bot 帳號是 `樂奈#7426`。
