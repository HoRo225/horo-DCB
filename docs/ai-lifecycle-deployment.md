# AI 生命週期正式部署

只適用 `ssh horo-server`、Compose project `horo-dcb`、`/srv/horo-dcb/compose.yaml`。SDK 與 CLI 為 `0.156.1`；不變更登入、`.env` 或公開任何 HTTP port。所有命令在 Horo 執行；勿輸出 Compose 展開結果、環境、auth、mapping 或 SDK 回覆。

## 候選驗證與準備

先完成 source QA，再把候選 source、Dockerfile、Compose、tests、ops 送至 `/tmp/horo-ai-lifecycle-20260926/<SHA>/source`。`<SHA>` 必須是實際候選 commit 的完整 40 位小寫 SHA。下列變數只保存非敏感版本及路徑；不要改用 `main`。

```bash
sha=<SHA>
source_dir=/tmp/horo-ai-lifecycle-20260926/$sha/source
bash -n "$source_dir/ops/ai-lifecycle-release.sh"
python3 -c 'import ast,pathlib,sys; ast.parse(pathlib.Path(sys.argv[1]).read_text())' "$source_dir/tests/verify_ai_live.py"
bot_id=$(sudo -n docker compose -p horo-dcb -f /srv/horo-dcb/compose.yaml ps -q bot)
container_source=/tmp/horo-ai-lifecycle-$sha
sudo -n docker exec "$bot_id" mkdir -p "$container_source"
sudo -n docker cp "$source_dir/." "$bot_id:$container_source"
sudo -n docker exec -w "$container_source" "$bot_id" python -m unittest discover -s tests -p 'test_*.py'
sudo -n bash "$source_dir/ops/ai-lifecycle-release.sh" prepare "$sha" "$source_dir"
```

回歸測試只使用正式容器內的暫存 mapping，不替換 `/app/codex` 或 `/app/data`。`prepare` 在停機前建置 immutable image，分別保存 Bot／Codex 的舊 image ID、回滾標籤、Compose 及固定 bind mount 路徑。它不停止服務。部署目錄與備份保持 `0700`／`0600`。

## 切換與驗收

```bash
release=/srv/horo-dcb-releases/$sha
sudo -n bash "$release/source/ops/ai-lifecycle-release.sh" deploy "$sha" "$release/source"
sudo -n bash "$release/source/ops/ai-lifecycle-release.sh" status "$sha" "$release/source"
```

切換先停 Bot（最多 40 秒）再停 Codex（最多 10 秒）；確認 writers 停止後才備份兩個資料目錄並遷移 v2。第 3 分鐘未完成 Bridge ready／Discord 登入就自動回滾，整體期限為 5 分鐘。若備份資料過大而超時，同樣回滾，不略過備份。正常 SDK／Discord 驗收仍需另外完成，腳本成功不代表全部驗收成功。

開始最多 15 分鐘的故障驗收窗口，記錄起始時間；第 12 分鐘仍有阻擋問題就執行回滾，保留 3 分鐘恢復。先用 `cua-driver` 確認 session 與 Discord 畫面，觀察專用測試父頻道 ID；不得變更正式白名單、刪除既有頻道或向其他成員發送訊息。

```bash
bot_id=$(sudo -n docker compose -p horo-dcb -f /srv/horo-dcb/compose.yaml ps -q bot)
parent_id=<Cua-observed-test-parent-channel-id>
sudo -n docker exec "$bot_id" python /tmp/verify_ai_live.py smoke --parent-channel-id "$parent_id"
sudo -n docker exec "$bot_id" python /tmp/verify_ai_live.py cancel --parent-channel-id "$parent_id"
```

驗證工具只使用合成測試 key，`finally` 精準封存當次 key。它從 Bot 環境取得 token，在記憶體傳遞，不讀登入檔或列印 SDK 回覆。取消案例保持連線 2 秒，關閉 HTTP socket，再確認 30 秒內專用 scope 已取消／detach 且服務回到 ready；容器重啟可接受。只有同 scope 的 archive 可以重試，chat 不重送，也不要求其他使用者的 active 計數為零。真正的執行中取消仍需觀察 Discord 專用對話與服務結果。

另用 `sdk-cancel` 驗證公開 SDK 的真實中斷契約。它在正式 Codex 容器開啟獨立驗證程序，沿用既有 `CODEX_HOME` 與登入，不替換 Bridge 的 client。使用暫存工作目錄、相同安全設定與專用 SDK thread；只有取得 turn handle，再收到該 turn 的 `interrupted` 終止事件，且精準封存與 public close 完成才算通過。`completed`／`failed` 不能當作取消證據，測試不重送 turn，也不輸出內容或識別碼。

```bash
codex_id=$(sudo -n docker compose -p horo-dcb -f /srv/horo-dcb/compose.yaml ps -q codex)
sudo -n docker cp "$release/source/tests/verify_ai_live.py" "$codex_id:/tmp/verify_ai_live.py"
sudo -n docker exec "$codex_id" timeout --kill-after=2s 60s python /tmp/verify_ai_live.py sdk-cancel
```

SDK 操作共用 45 秒期限，其中預留 5 秒封存、5 秒關閉與 collector 收尾；RPC 限時等待不會被當作 SDK 已停止。容器內 `timeout` 是 60 秒失敗兜底，超時不算驗收通過。這項結果補足直接 SDK 的中斷證據，Bridge／Discord 的正常回覆、HTTP 取消與隔離驗收仍須分別完成。

若精準清理失敗，工具保留私有 `/tmp/horo-ai-live-<synthetic-id>.json`。只對該工具建立、owner 相同、權限 `0600` 的 manifest 執行：

```bash
sudo -n docker exec "$bot_id" python /tmp/verify_ai_live.py cleanup --manifest /tmp/horo-ai-live-<synthetic-id>.json
```

另外透過專用 Discord 對話驗證正常回覆、同對話輸出順序、執行中取消，以及 Codex 停止／恢復期間行事曆、Steam、語音與控制台仍能運作。每次停止或重啟前確認故障窗口剩餘時間；不修改正式資料來模擬損壞。

```bash
dc=(sudo -n docker compose --project-directory /srv/horo-dcb --env-file /srv/horo-dcb/.env -p horo-dcb -f /srv/horo-dcb/compose.yaml -f /srv/horo-dcb/ai-lifecycle-active.yaml)
"${dc[@]}" stop -t 10 codex
# Observe the other Discord features with cua-driver, then restore Codex promptly.
"${dc[@]}" up -d --no-build codex
sudo -n docker exec "$bot_id" python /tmp/verify_ai_live.py ready
```

## 回滾

```bash
sudo -n bash "$release/source/ops/ai-lifecycle-release.sh" rollback "$sha" "$release/source"
```

回滾先停止 writers，以候選 image 將 **CURRENT** v2 mapping 投影 v1，再分別啟動兩個舊 image。不還原部署前 mapping、不整卷覆蓋 Bot 資料，不復活已 detach 對話。mapping 無法驗證時保留原檔，透過 Compose override 停用 Bot AI，其他功能照常啟動。回滾保留無 readiness 依賴的 Compose，Codex healthcheck 使用舊 `/healthz`。

目前啟用的 image override 保存於 `/srv/horo-dcb/ai-lifecycle-active.yaml`；後續手動 `up` 必須使用它，否則基本 Compose 的 `horo-dcb:local` 會覆蓋選定版本：

```bash
sudo -n docker compose --project-directory /srv/horo-dcb --env-file /srv/horo-dcb/.env -p horo-dcb -f /srv/horo-dcb/compose.yaml -f /srv/horo-dcb/ai-lifecycle-active.yaml up -d --no-build
```

保留兩個舊 image 與私有備份，直到驗收通過。全程禁止 `down -v`、volume 刪除、image prune、整卷還原或將私人資料加入 Git。
