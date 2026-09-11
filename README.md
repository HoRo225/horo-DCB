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
- 測試只存在 repository 與雲端 runner；正式 image 不包含 `tests/`。

## 開發與測試

開發使用隔離的 Codex Cloud environment；CI、staging 與 image build 使用 GitHub-hosted runners，不依賴個人電腦。

```sh
docker build --target test .
docker build --target runtime -t horo-dcb:runtime .
```

Pull Request 必須通過完整 unittest suite 與 runtime image 邊界檢查。獨立 staging Bot 由 GitHub `staging` environment 手動啟動，所有 staging 憑證只在一次性 runner 使用。

## 部署

`main` 通過 CI 後會發布公開、不可變的 GHCR image，production 只按 digest 部署：

```dotenv
HORO_DCB_IMAGE=ghcr.io/horo225/horo-dcb@sha256:<digest>
```

正式主機只需要 Docker、Compose、`compose.yaml`、私有 `.env` 與 named volumes，不需要 Git checkout、測試或 build tools。

## 首次私有設定

1. 在正式主機 `.env` 填入 Discord token、Guild 設定及精確 image digest。
2. 將 Codex base instructions 寫入 `codex_data` 的 `/app/codex/base_instructions.txt`；檔案須為 UTF-8、非空、一般檔案、權限 `0600`，且不超過 16 KiB。
3. 以 Compose 執行 `python -m src.codex_bridge login` 完成 Codex 登入。
4. 執行 `sh scripts/check-env.sh` 後再啟動服務。

本 repository 不包含任何實際秘密、prompt 或正式部署資料。

## 授權

此 repository 未提供軟體授權。除 GitHub 服務條款允許的檢視與 fork 外，著作權人保留所有權利；目前不接受外部貢獻。
