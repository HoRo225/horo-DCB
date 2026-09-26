#!/usr/bin/env bash
# This release helper intentionally supports only the Horo AI lifecycle rollout.
set -Eeuo pipefail
umask 077

fail() { printf '%s\n' "$1" >&2; exit 1; }
[[ $EUID == 0 ]] || fail 'Run with sudo -n bash.'
[[ $# == 3 ]] || fail 'Usage: MODE SHA SOURCE_DIR'
mode=$1
sha=$2
[[ $mode =~ ^(prepare|deploy|rollback|status)$ && $sha =~ ^[0-9a-f]{40}$ ]] || fail 'Invalid mode or release SHA.'
source_dir=$(realpath -e -- "$3")
release=/srv/horo-dcb-releases/$sha
[[ $(realpath -m -- "$release") == "$release" ]] || fail 'Release path must not follow symlinks.'
case "$source_dir" in
    /tmp/horo-ai-lifecycle-20260926/*|"$release/source") ;;
    *) fail 'Source must be inside the approved staging directory or this release.' ;;
esac
[[ -d $source_dir && -f $source_dir/Dockerfile && -f $source_dir/compose.yaml \
    && -f $source_dir/tests/verify_ai_live.py && -f $source_dir/src/ai/thread_store.py ]] || fail 'Incomplete candidate source.'
live=/srv/horo-dcb
compose=$live/compose.yaml
image=horo-dcb:$sha
if [[ $mode != status ]]; then
    exec 9> "$live/ai-lifecycle.lock"
    flock -n 9 || fail 'Another lifecycle operation is in progress.'
fi
start=$SECONDS
changed=0
recovering=0
rollback_end=300
if [[ $mode == rollback ]]; then rollback_end=180; fi
dc() { docker compose --project-directory "$live" --env-file "$live/.env" -p horo-dcb -f "$compose" "$@"; }
current_id() {
    local cid
    cid=$(dc ps -aq "$1")
    [[ $cid =~ ^[0-9a-f]{64}$ ]] || return 1
    docker inspect --format '{{.Image}}' "$cid"
}
rollback_image() {
    local service=$1 live_image=$2 available_image
    if ! available_image=$(docker image inspect --format '{{.Id}}' "$live_image" 2>/dev/null); then
        available_image=$(docker image inspect --format '{{.Id}}' "horo-dcb:recovered-$service" 2>/dev/null) \
            || fail 'Original image missing; prebuilt recovery image required before prepare.'
        printf 'Using the prebuilt recovery image for %s rollback.\n' "$service" >&2
    fi
    [[ $available_image =~ ^sha256:[0-9a-f]{64}$ ]] || fail 'Invalid rollback image ID.'
    printf '%s\n' "$available_image"
}
within() {
    local end=$1
    shift
    local remaining=$((start + end - SECONDS))
    # Reserve the forced-kill grace inside the original phase cutoff.
    (( remaining > 2 )) || return 1
    if [[ $1 == dc ]]; then
        shift
        set -- docker compose --project-directory "$live" --env-file "$live/.env" -p horo-dcb -f "$compose" "$@"
    fi
    timeout --foreground --kill-after=2s "$((remaining - 2))s" "$@"
}
bot_logged_in() {
    local end=$1 cid=$2 running started_at
    running=$(within "$end" timeout 3s docker inspect --format '{{.State.Running}}' "$cid") || return 1
    [[ $running == true ]] || return 1
    started_at=$(within "$end" timeout 3s docker inspect --format '{{.State.StartedAt}}' "$cid") || return 1
    within "$end" timeout 3s docker logs --since "$started_at" "$cid" 2>&1 \
        | grep -F 'Discord Bot 已登入：' >/dev/null
}
migration_cleanup() {
    local end=$1 name=$2 cid label
    # Only a successful daemon listing can establish absence; inspect errors cannot.
    cid=$(within "$end" docker container ls -aq --no-trunc --filter "name=^/${name}$") || return 2
    [[ -n $cid ]] || return 0
    [[ $cid =~ ^[0-9a-f]{64}$ ]] || return 2
    label=$(within "$end" docker inspect --format '{{index .Config.Labels "horo.ai-lifecycle.release"}}' "$cid") || return 2
    [[ $label == "$sha" ]] || return 2
    within "$end" docker container rm -f "$cid" >/dev/null || return 2
    return 1
}
migrate() {
    local name=horo-dcb-migrate-$sha result=0 cleanup_result=0
    if migration_cleanup "$1" "$name"; then
        :
    else
        cleanup_result=$?
        (( cleanup_result != 2 )) || return 2
    fi
    within "$1" timeout --foreground --kill-after=1s 10s docker run --rm --name "$name" \
        --label "horo.ai-lifecycle.release=$sha" --network none \
        --mount type=bind,src=/srv/horo-dcb-data/codex,dst=/app/codex \
        --entrypoint python "$image" -m src.ai.thread_store migrate \
        --path /app/codex/horo_threads.json --target-version "$2" || result=$?
    # Killing the Docker client does not stop its container; confirm no writer remains.
    if migration_cleanup "$1" "$name"; then
        :
    else
        cleanup_result=$?
        (( cleanup_result != 2 )) || return 2
        result=1
    fi
    return "$result"
}
load_release() {
    [[ -f $release/prepared && -f $release/bot.image && -f $release/codex.image \
        && -f $release/bot.live.image && -f $release/codex.live.image ]] || fail 'Release was not prepared.'
    read -r bot_image < "$release/bot.image"
    read -r codex_image < "$release/codex.image"
    read -r bot_live_image < "$release/bot.live.image"
    read -r codex_live_image < "$release/codex.live.image"
    [[ $bot_image =~ ^sha256:[0-9a-f]{64}$ && $codex_image =~ ^sha256:[0-9a-f]{64}$ \
        && $bot_live_image =~ ^sha256:[0-9a-f]{64}$ && $codex_live_image =~ ^sha256:[0-9a-f]{64}$ ]] || fail 'Invalid image manifest.'
    docker image inspect "$bot_image" "$codex_image" "$image" >/dev/null
    [[ $(docker image inspect --format '{{.Id}}' "$image") == "$(cat "$release/candidate.image")" ]] || fail 'Candidate image changed.'
}
rollback_impl() {
    recovering=1
    within "$rollback_end" dc stop -t 40 bot >/dev/null || return 1
    within "$rollback_end" dc stop -t 10 codex >/dev/null || return 1
    local running
    running=$(within "$rollback_end" dc ps -q --status running) || return 1
    [[ -z $running ]] || return 1
    local disable_ai=0 migration_result=0
    if migrate "$rollback_end" 1; then
        :
    else
        # A foreign/unconfirmed migration writer must prevent either old service starting.
        migration_result=$?
        (( migration_result != 2 )) || return 2
        disable_ai=1
        printf '%s\n' 'Mapping is invalid; original file retained and Bot AI disabled.' >&2
    fi
    # The candidate compose has no readiness dependency; old images retain it too.
    cp -- "$release/source/compose.yaml" "$compose" || return 1
    cat > "$release/rollback.yaml" <<EOF || return 1
services:
  bot:
    image: $bot_image
EOF
    if (( disable_ai )); then
        cat >> "$release/rollback.yaml" <<'EOF' || return 1
    environment:
      CODEX_ENABLED: '0'
EOF
    fi
    cat >> "$release/rollback.yaml" <<EOF || return 1
  codex:
    image: $codex_image
    healthcheck:
      test: [CMD, python, -c, "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=5)"]
EOF
    within "$rollback_end" dc -f "$release/rollback.yaml" up -d --no-build codex >/dev/null || return 1
    within "$rollback_end" dc -f "$release/rollback.yaml" up -d --no-build bot >/dev/null || return 1
    cp -- "$release/rollback.yaml" "$live/ai-lifecycle-active.yaml" || return 1
    local cid
    cid=$(within "$rollback_end" dc ps -q bot) || return 1
    until bot_logged_in "$rollback_end" "$cid"; do
        (( SECONDS - start < rollback_end )) || return 1
        sleep 1
    done
    changed=0
    printf '%s\n' 'Rollback restored Discord with each previous image; current mapping retained.'
}
on_exit() {
    local result=$?
    trap - EXIT INT TERM
    if (( result != 0 && changed && ! recovering )); then
        rollback_impl || printf '%s\n' 'Automatic rollback failed; inspect service state immediately.' >&2
    fi
    exit "$result"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

case "$mode" in
prepare)
    [[ ! -e $release/prepared ]] || fail 'Release is already prepared.'
    [[ $source_dir != "$release/source" ]] || fail 'Prepare from staging, not the release copy.'
    [[ ! -e $release/source ]] || fail 'Release source already exists.'
    grep -qx 'openai-codex==0.156.1' "$source_dir/requirements.txt"
    grep -qx 'openai-codex-cli-bin==0.156.1' "$source_dir/requirements.txt"
    if grep -Eq '^[[:space:]]*(depends_on|ports|network_mode):' "$source_dir/compose.yaml"; then
        fail 'Candidate compose must not add ports or depend on AI readiness.'
    fi
    grep -q '/livez' "$source_dir/compose.yaml"
    grep -q 'device: /srv/horo-dcb-data/bot' "$source_dir/compose.yaml"
    grep -q 'device: /srv/horo-dcb-data/codex' "$source_dir/compose.yaml"
    bot_live_image=$(current_id bot)
    codex_live_image=$(current_id codex)
    [[ $bot_live_image =~ ^sha256:[0-9a-f]{64}$ && $codex_live_image =~ ^sha256:[0-9a-f]{64}$ ]] || fail 'Current images unavailable.'
    bot_image=$(rollback_image bot "$bot_live_image")
    codex_image=$(rollback_image codex "$codex_live_image")
    for service in bot codex; do
        volume=horo-dcb_${service}_data_srv
        [[ $(docker volume inspect --format '{{index .Options "device"}}' "$volume") == "/srv/horo-dcb-data/$service" ]] || fail 'Unexpected data mount.'
        cid=$(dc ps -aq "$service")
        destination=/app/codex
        if [[ $service == bot ]]; then destination=/app/data; fi
        actual_volume=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "'"$destination"'"}}{{.Name}}{{end}}{{end}}' "$cid")
        [[ $actual_volume == "$volume" ]] || fail 'Unexpected live container mount.'
    done
    if docker image inspect "$image" >/dev/null 2>&1; then fail 'Candidate tag already exists.'; fi
    mkdir -p -- "$release"
    chmod 700 "$release"
    cp -a -- "$source_dir" "$release/source"
    cp -- "$compose" "$release/compose.before.yaml"
    printf '%s\n' "$bot_image" > "$release/bot.image"
    printf '%s\n' "$codex_image" > "$release/codex.image"
    printf '%s\n' "$bot_live_image" > "$release/bot.live.image"
    printf '%s\n' "$codex_live_image" > "$release/codex.live.image"
    printf '%s\n' "$compose" '/srv/horo-dcb/.env' '/srv/horo-dcb-data/bot' '/srv/horo-dcb-data/codex' > "$release/paths.txt"
    printf '%s\n' 'horo-dcb_bot_data_srv /app/data /srv/horo-dcb-data/bot' \
        'horo-dcb_codex_data_srv /app/codex /srv/horo-dcb-data/codex' > "$release/mounts.txt"
    docker tag "$bot_image" "horo-dcb:rollback-bot-$sha"
    docker tag "$codex_image" "horo-dcb:rollback-codex-$sha"
    docker build --tag "$image" "$release/source"
    docker image inspect --format '{{.Id}}' "$image" > "$release/candidate.image"
    printf 'services:\n  bot:\n    image: %s\n  codex:\n    image: %s\n' "$image" "$image" > "$release/deploy.yaml"
    touch "$release/prepared"
    printf 'Prepared release %s\n' "$sha"
    ;;
deploy)
    load_release
    [[ $(current_id bot) == "$bot_live_image" && $(current_id codex) == "$codex_live_image" ]] || fail 'Live images changed after prepare.'
    [[ ! -e $release/backups ]] || fail 'Deployment backup already exists; inspect the previous attempt.'
    start=$SECONDS
    changed=1
    within 180 dc stop -t 40 bot >/dev/null
    within 180 dc stop -t 10 codex >/dev/null
    running=$(within 180 dc ps -q --status running)
    [[ -z $running ]] || fail 'Writers remain running.'
    mkdir -m 700 -- "$release/backups"
    within 180 cp -- "$compose" "$live/.env" "$release/backups/"
    within 180 tar -C /srv/horo-dcb-data -cpf "$release/backups/bot.tar" bot
    within 180 tar -C /srv/horo-dcb-data -cpf "$release/backups/codex.tar" codex
    chmod 600 "$release/backups/"*
    migrate 180 2
    cp -- "$release/source/compose.yaml" "$compose"
    within 180 dc -f "$release/deploy.yaml" up -d --no-build codex >/dev/null
    within 180 dc -f "$release/deploy.yaml" up -d --no-build bot >/dev/null
    bot_id=$(within 180 dc ps -q bot)
    within 180 docker cp "$release/source/tests/verify_ai_live.py" "$bot_id:/tmp/verify_ai_live.py"
    within 180 docker exec "$bot_id" python /tmp/verify_ai_live.py ready
    until bot_logged_in 180 "$bot_id"; do
        (( SECONDS - start < 180 )) || fail 'Discord startup cutoff reached.'
        sleep 1
    done
    cp -- "$release/deploy.yaml" "$live/ai-lifecycle-active.yaml"
    changed=0
    printf 'Deployed release %s in %ss; run Discord and SDK acceptance next.\n' "$sha" "$((SECONDS-start))"
    ;;
rollback)
    load_release
    start=$SECONDS
    changed=1
    rollback_impl
    ;;
status)
    for service in bot codex; do
        cid=$(dc ps -aq "$service")
        [[ $cid =~ ^[0-9a-f]{64}$ ]] || fail 'Service container unavailable.'
        printf '%s ' "$service"
        docker inspect --format '{{.State.Status}} {{.Image}}' "$cid"
    done
    ;;
esac
