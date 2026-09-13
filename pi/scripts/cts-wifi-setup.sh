#!/usr/bin/env bash
# Request Wi-Fi setup via the local Unix-socket controller.
# This does not use an unauthenticated HTTP endpoint.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export CTS_WIFI_PROVISIONING="${CTS_WIFI_PROVISIONING:-1}"

CLIENT_SCRIPT="${CTS_WIFI_CLIENT_SCRIPT:-/usr/local/lib/cts-scoreboard/wifi_provisioning_client.py}"
if [ ! -r "$CLIENT_SCRIPT" ]; then
    REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
    if [ -r "$REPO_DIR/wifi_provisioning_client.py" ] && [ -f "$REPO_DIR/start.sh" ]; then
        CLIENT_SCRIPT="$REPO_DIR/wifi_provisioning_client.py"
    else
        echo "cts-wifi-setup: missing provisioning client at $CLIENT_SCRIPT" >&2
        exit 1
    fi
fi

exec python3 "$CLIENT_SCRIPT" enter_setup
