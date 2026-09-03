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

random_hex() {
    byte_count=$1
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex "$byte_count"
        return
    fi
    od -An -N "$byte_count" -tx1 /dev/urandom | tr -d ' \n'
}

replace_env_value() {
    key=$1
    value=$2
    temporary_file=$(mktemp "${TMPDIR:-/tmp}/horo-dcb-env.XXXXXX")
    awk -v key="$key" -v value="$value" '
        BEGIN { replaced = 0 }
        index($0, key "=") == 1 {
            print key "=" value
            replaced = 1
            next
        }
        { print }
        END {
            if (!replaced) {
                print key "=" value
            }
        }
    ' .env > "$temporary_file"
    mv "$temporary_file" .env
}

if [ -e .env ]; then
    printf '%s\n' "Existing .env preserved; no values were overwritten."
else
    [ -f .env.example ] || fail ".env.example is missing."
    cp .env.example .env
    replace_env_value CODEX_BRIDGE_TOKEN "$(random_hex 32)"
    printf '%s\n' "Created .env with a local Codex bridge token."
fi
chmod 600 .env

docker compose config --quiet

cat <<'EOF'

Setup files are ready.

1. Edit .env and replace [REDACTED_SECRET] for DISCORD_TOKEN.
2. Build the shared image: docker compose build bot
3. Log in: docker compose run --rm --no-deps codex python -m src.codex_bridge login
4. Set CODEX_ALLOWED_GUILD_ID, CODEX_ALLOWED_CHANNEL_ID, and
   CODEX_ALLOWED_USER_IDS, then set CODEX_ENABLED=1.
5. Validate: sh scripts/check-env.sh
6. Start: docker compose up -d

Codex stores OAuth and persistent thread state only in codex_data. The Bot does
not mount that volume. Fresh installs keep voice, Steam automation, and Server
Activity disabled until their documented switches are enabled.
EOF
