#!/bin/sh

set -eu

SCRIPT_PATH=$0
while [ -L "$SCRIPT_PATH" ]; do
    SCRIPT_DIR=$(CDPATH= cd -P -- "$(dirname -- "$SCRIPT_PATH")" && pwd)
    LINK_TARGET=$(readlink "$SCRIPT_PATH")
    case "$LINK_TARGET" in
        /*) SCRIPT_PATH=$LINK_TARGET ;;
        *) SCRIPT_PATH=$SCRIPT_DIR/$LINK_TARGET ;;
    esac
done
PROJECT_DIR=$(CDPATH= cd -P -- "$(dirname -- "$SCRIPT_PATH")" && pwd)
SAN_DISK_VOLUME=${SAN_DISK_VOLUME:-"/Volumes/SanDisk Mac AI"}
BASE_URL=${BASE_URL:-"http://127.0.0.1:8000"}

if [ ! -d "$SAN_DISK_VOLUME" ] || ! /usr/sbin/diskutil info "$SAN_DISK_VOLUME" 2>/dev/null | grep -Eq 'Mounted:[[:space:]]+Yes'; then
    printf 'ERROR: Required external disk is not mounted: %s\n' "$SAN_DISK_VOLUME" >&2
    printf 'Connect and mount the SanDisk external disk, then run this script again.\n' >&2
    exit 1
fi

if [ -x "$PROJECT_DIR/.venv/bin/python" ] && "$PROJECT_DIR/.venv/bin/python" -c 'import uvicorn' 2>/dev/null; then
    PYTHON="$PROJECT_DIR/.venv/bin/python"
else
    PYTHON=${PYTHON:-python}
fi

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    printf 'ERROR: Python interpreter not found: %s\n' "$PYTHON" >&2
    exit 1
fi

if ! "$PYTHON" -c 'import uvicorn' 2>/dev/null; then
    printf 'ERROR: Required Python packages are not installed for: %s\n' "$PYTHON" >&2
    printf 'Install the project dependencies, then run this script again.\n' >&2
    exit 1
fi

printf 'SanDisk prerequisite passed: %s\n\n' "$SAN_DISK_VOLUME"
printf 'AI Media Workflow\n'
printf '  Main UI:             %s/\n' "$BASE_URL"
printf '  API documentation:   %s/docs\n' "$BASE_URL"
printf '  OpenAPI schema:       %s/openapi.json\n' "$BASE_URL"
printf '  List blocks:          GET  %s/api/blocks/\n' "$BASE_URL"
printf '  Block detail:         GET  %s/api/blocks/{name}\n' "$BASE_URL"
printf '  Run workflow:         POST %s/api/workflows/run\n' "$BASE_URL"
printf '  List jobs:            GET  %s/api/workflows/jobs\n' "$BASE_URL"
printf '  Job detail:           GET  %s/api/workflows/jobs/{id}\n\n' "$BASE_URL"
printf 'Starting server with %s...\n' "$("$PYTHON" -c 'import sys; print(sys.executable)')"

cd "$PROJECT_DIR"
exec "$PYTHON" main.py
