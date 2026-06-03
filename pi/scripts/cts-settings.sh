#!/usr/bin/env bash
# Open the CTS Scoreboard /settings page in a regular Chromium window,
# using a dedicated profile so it does not collide with the kiosk
# Chromium singleton.

set -euo pipefail

URL="${CTS_SETTINGS_URL:-http://localhost:5000/settings}"
PROFILE_DIR="${CTS_SETTINGS_PROFILE:-$HOME/.config/cts-settings-chromium}"

mkdir -p "$PROFILE_DIR"

CHROMIUM="$(command -v chromium || command -v chromium-browser || true)"
if [ -z "$CHROMIUM" ]; then
    echo "cts-settings: chromium is not installed." >&2
    exit 1
fi

exec "$CHROMIUM" \
    --user-data-dir="$PROFILE_DIR" \
    --password-store=basic \
    --use-mock-keychain \
    --no-first-run \
    --new-window \
    "$URL"
