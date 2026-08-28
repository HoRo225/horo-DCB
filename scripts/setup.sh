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
    replace_env_value NINEROUTER_IMAGE_SEARCH_PROVIDER ""
    replace_env_value JWT_SECRET "$(random_hex 32)"
    replace_env_value INITIAL_PASSWORD "$(random_hex 16)"
    replace_env_value API_KEY_SECRET "$(random_hex 32)"
    replace_env_value MACHINE_ID_SALT "$(random_hex 32)"
    printf '%s\n' "Created .env with locally generated 9Router secrets."
fi
chmod 600 .env

docker compose config --quiet

cat <<'EOF'

Setup files are ready.

1. Edit .env and replace [REDACTED_SECRET] for DISCORD_TOKEN.
2. Start only 9Router: docker compose up -d --build 9router
3. Open http://127.0.0.1:20128, sign in with INITIAL_PASSWORD from the local .env,
   change that password, then configure AI, Search, Fetch providers, and aliases.
4. Create a 9Router API key and replace NINEROUTER_API_KEY in .env.
5. Optional image search: set NINEROUTER_IMAGE_SEARCH_PROVIDER to a provider alias
   that returns image URLs. Blank reuses NINEROUTER_WEB_SEARCH_PROVIDER.
6. Validate: sh scripts/check-env.sh
7. Start the full stack: docker compose up -d --build

Semantic Memory and Server Activity are disabled in a fresh setup. Review the
README privacy and Discord prerequisites before setting SEMANTIC_MEMORY_ENABLED=1
or SERVER_ACTIVITY_ENABLED=1.
EOF
