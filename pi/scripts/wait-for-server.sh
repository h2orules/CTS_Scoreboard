#!/usr/bin/env bash
# Poll the local CTS Scoreboard HTTP endpoint until it responds, or until
# the timeout elapses. Used by cts-kiosk.sh before launching Chromium.
#
# Usage: wait-for-server.sh [URL] [TIMEOUT_SECONDS]
#   URL              default: http://127.0.0.1:5000/web/home
#   TIMEOUT_SECONDS  default: 60

set -euo pipefail

URL="${1:-http://127.0.0.1:5000/web/home}"
TIMEOUT="${2:-60}"

start=$(date +%s)
while true; do
    if curl --silent --fail --output /dev/null --max-time 2 "$URL"; then
        exit 0
    fi
    now=$(date +%s)
    if (( now - start >= TIMEOUT )); then
        echo "wait-for-server: timed out after ${TIMEOUT}s waiting for $URL" >&2
        exit 1
    fi
    sleep 1
done
