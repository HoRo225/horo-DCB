# horo-DCB

Python Discord Bot，使用 `discord.py`、`uv` 與 Docker Compose。

## Requirements

- Python 3.13
- uv 0.10.x
- Docker + Docker Compose（部署時）

## Local setup

```bash
uv sync --locked
mkdir -p .secrets
chmod 700 .secrets
```

將 Discord Bot token 寫入 `.secrets/discord_token`，並限制檔案權限：

```bash
chmod 600 .secrets/discord_token
```

`.secrets/` 已被 Git 與 Docker build context 排除。

## Run locally

```bash
DISCORD_TOKEN_FILE=.secrets/discord_token uv run python -m horo_dcb
```

## Tests

```bash
uv run python -m unittest discover -s tests -v
```

## Docker deployment

建立 secret 後：

```bash
docker compose build
docker compose up -d
docker compose logs -f bot
```

停止服務：

```bash
docker compose down
```

Compose project name 固定為 `horo-dcb`，第一版只有 `bot` service，沒有對外 ports 或持久化 volumes。

## Project layout

```text
horo-DCB/
├── .dockerignore
├── .gitignore
├── .python-version
├── Dockerfile
├── README.md
├── compose.yaml
├── pyproject.toml
├── uv.lock
├── horo_dcb/
│   ├── __init__.py
│   ├── __main__.py
│   ├── bot.py
│   └── config.py
└── tests/
    ├── test_bot.py
    ├── test_config.py
    └── test_main.py
```

Discord 功能實際出現後才新增 `horo_dcb/cogs/`；目前不預先建立 service、database、worker 或其他未使用的架構層。
