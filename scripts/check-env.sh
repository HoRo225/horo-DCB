#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_DIR"

errors=0

report_error() {
    printf '%s\n' "configuration error: $*" >&2
    errors=$((errors + 1))
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

is_true() {
    normalized=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
    case "$normalized" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

validate_positive_id() {
    key=$1
    value=$(read_env_value "$key")
    case "$value" in
        ""|0*|*[!0-9]*) report_error "$key must be a positive integer." ;;
    esac
}

[ -f .env ] || {
    printf '%s\n' "configuration error: .env is missing; run sh scripts/setup.sh first." >&2
    exit 1
}

require_configured DISCORD_TOKEN
require_configured CODEX_BRIDGE_TOKEN

bridge_token=$(read_env_value CODEX_BRIDGE_TOKEN)
if [ "${#bridge_token}" -ne 64 ]; then
    report_error "CODEX_BRIDGE_TOKEN must contain 64 lowercase hexadecimal characters."
else
    case "$bridge_token" in
        *[!0-9a-f]*) report_error "CODEX_BRIDGE_TOKEN must contain 64 lowercase hexadecimal characters." ;;
    esac
fi

for key in \
    CODEX_ENABLED \
    TEMP_VOICE_ENABLED \
    STEAM_FREE_GAMES_ENABLED \
    AI_TEXT_DISPLAY_ENABLED
do
    value=$(read_env_value "$key")
    normalized=$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')
    case "$normalized" in
        ""|0|1|true|false|yes|no|on|off) ;;
        *) report_error "$key must be a documented boolean value." ;;
    esac
done

if is_true "$(read_env_value CODEX_ENABLED)"; then
    validate_positive_id CODEX_ALLOWED_GUILD_ID
    channel=$(read_env_value CODEX_ALLOWED_CHANNEL_ID)
    if [ -n "$channel" ]; then
        validate_positive_id CODEX_ALLOWED_CHANNEL_ID
    fi
    users=$(read_env_value CODEX_ALLOWED_USER_IDS)
    if [ -n "$users" ] && ! printf '%s\n' "$users" | awk -F, '
        NF == 0 { exit 1 }
        {
            delete seen
            for (field = 1; field <= NF; field++) {
                value = $field
                gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
                if (value !~ /^[1-9][0-9]*$/ || seen[value]++) {
                    exit 1
                }
            }
        }
    '; then
        report_error "CODEX_ALLOWED_USER_IDS must be unique comma-separated positive integers."
    fi
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
