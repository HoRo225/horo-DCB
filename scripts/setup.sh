#!/bin/sh
set -eu
umask 077

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_DIR"

fail() {
    printf '%s\n' "setup error: $*" >&2
    exit 1
}

command -v docker >/dev/null 2>&1 || fail "Docker is required."
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required."

if [ -e .env ]; then
    printf '%s\n' "Existing .env preserved; no values were overwritten."
else
    [ -f .env.example ] || fail ".env.example is missing."
    cp .env.example .env
    token=$(od -An -N 32 -tx1 /dev/urandom | tr -d ' \n')
    sed -i "s/^CODEX_BRIDGE_TOKEN=.*/CODEX_BRIDGE_TOKEN=$token/" .env
    printf '%s\n' "Created .env with a local Codex bridge token."
fi
chmod 600 .env

docker compose -f compose.yaml config --quiet

cat <<'EOF'

Setup files are ready.

1. Edit .env and replace [REDACTED_SECRET] for DISCORD_TOKEN.
2. In CI/disposable build environments only: docker compose -f compose.yaml -f compose.build.yaml build bot
   For deployment, load the tested CI image archive instead.
3. Store private base instructions as /app/codex/base_instructions.txt in codex_data
   with mode 0600. This repository intentionally does not include that file.
4. Log in: docker compose -f compose.yaml run --rm --no-deps codex python -m src.codex_bridge login
5. Set CODEX_ALLOWED_GUILD_ID, then set CODEX_ENABLED=1.
6. Validate: sh scripts/check-env.sh
7. Start: docker compose -f compose.yaml up -d
8. In Discord, use /控制台 → AI 助手 to select 1-25 text channels and roles.

Codex stores OAuth and persistent thread state only in codex_data. The Bot does
not mount that volume. The selected AI channels and roles are stored in bot_data. Fresh
installs keep voice and Steam automation disabled until their documented switches
are enabled. AI requires Server Members Intent in the Discord Developer Portal.
EOF
