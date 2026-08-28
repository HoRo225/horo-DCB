#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_DIR"

errors=0

report_error() {
    printf '%s\n' "configuration error: $*" >&2
    errors=$((errors + 1))
}

has_env_key() {
    key=$1
    awk -v key="$key" '
        index($0, key "=") == 1 { found = 1 }
        END { exit !found }
    ' .env
}

read_env_value() {
    key=$1
    awk -v key="$key" '
        index($0, key "=") == 1 {
            sub("^[^=]*=", "")
            value = $0
        }
        END {
            sub("^[[:space:]]+", "", value)
            sub("[[:space:]]+$", "", value)
            print value
        }
    ' .env
}

require_configured() {
    key=$1
    value=$(read_env_value "$key")
    case "$value" in
        ""|"[REDACTED_SECRET]"*|__REQUIRED*|__GENERATE*)
            report_error "$key is not configured."
            ;;
    esac
}

[ -f .env ] || {
    printf '%s\n' "configuration error: .env is missing; run sh scripts/setup.sh first." >&2
    exit 1
}

for key in \
    DISCORD_TOKEN \
    NINEROUTER_API_KEY \
    NINEROUTER_MODEL \
    NINEROUTER_WEB_SEARCH_PROVIDER \
    NINEROUTER_WEB_FETCH_PROVIDER \
    JWT_SECRET \
    INITIAL_PASSWORD \
    API_KEY_SECRET \
    MACHINE_ID_SALT
do
    require_configured "$key"
done

for key in \
    SEMANTIC_MEMORY_ENABLED \
    TEMP_VOICE_ENABLED \
    STEAM_FREE_GAMES_ENABLED \
    AI_TEXT_DISPLAY_ENABLED \
    SERVER_ACTIVITY_ENABLED
do
    value=$(read_env_value "$key")
    normalized=$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')
    case "$normalized" in
        ""|0|1|true|false|yes|no|on|off) ;;
        *) report_error "$key must be a documented boolean value." ;;
    esac
done

if has_env_key NINEROUTER_EMBEDDING_MODEL; then
    embedding_model=$(read_env_value NINEROUTER_EMBEDDING_MODEL)
    [ -n "$embedding_model" ] || report_error "NINEROUTER_EMBEDDING_MODEL is empty."
fi

if has_env_key NINEROUTER_EMBEDDING_DIMENSIONS; then
    dimensions=$(read_env_value NINEROUTER_EMBEDDING_DIMENSIONS)
    case "$dimensions" in
        ""|0|*[!0-9]*)
            report_error "NINEROUTER_EMBEDDING_DIMENSIONS must be a positive integer."
            ;;
    esac
fi

if [ "$errors" -ne 0 ]; then
    exit 1
fi

command -v docker >/dev/null 2>&1 || {
    printf '%s\n' "configuration error: Docker is required." >&2
    exit 1
}
docker compose version >/dev/null 2>&1 || {
    printf '%s\n' "configuration error: Docker Compose v2 is required." >&2
    exit 1
}
docker compose config --quiet

printf '%s\n' "Environment and Compose configuration are valid."
