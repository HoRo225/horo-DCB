# horo-DCB

horo-DCB 是以 Python、Discord Bot 與獨立 Codex sidecar 組成的繁體中文服務。

## 功能

- Discord 管理控制台
- Guild、文字頻道與身分組限定的 Codex 對話
- Discord 行事曆看板
- 臨時語音頻道
- Steam 限時免費遊戲通知

## 安全邊界

- Bot 不掛載 Codex OAuth 資料。
- Codex sidecar 不對主機發布連接埠，並以 Bearer token 驗證內部請求。
- Codex 使用唯讀 sandbox、deny-all approval，且停用 Shell、MCP、Apps、Subagents 與全域 Memories。
- 使用者輸入、圖片及網路搜尋結果均視為不可信資料。
- `.env`、OAuth、Discord 識別碼、prompt、對話及執行狀態不得提交至 Git。

## 使用需求

- Docker
- Docker Compose v2
- Discord application 與 Bot token
- 可使用 Codex 的 ChatGPT 帳號

## 設定

```sh
sh scripts/setup.sh
```

接著在本機完成下列私有設定：

1. 在 `.env` 填入 Discord token 與 Guild 設定。
2. 將 Codex base instructions 寫入 `codex_data` 的 `/app/codex/base_instructions.txt`，檔案須為 UTF-8、非空、一般檔案、權限 `0600`，且不超過 16 KiB。
3. 執行 `python -m src.codex_bridge login` 的 Compose 指令完成 Codex 登入。
4. 執行 `sh scripts/check-env.sh` 後再啟動服務。

本 repository 不包含任何實際秘密、prompt 或正式部署資料。

## 測試

```sh
docker compose -f compose.yaml -f compose.build.yaml build bot
docker run --rm --entrypoint python horo-dcb:local -m unittest discover -s tests -t . -v
```

## 授權

此 repository 未提供軟體授權。除 GitHub 服務條款允許的檢視與 fork 外，著作權人保留所有權利；目前不接受外部貢獻。
