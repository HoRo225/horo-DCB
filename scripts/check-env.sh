#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_DIR"

docker compose -f compose.yaml config --quiet
docker compose -f compose.yaml run --rm -T --no-deps bot python -m src.preflight
