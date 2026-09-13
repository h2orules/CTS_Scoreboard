#!/usr/bin/env bash
# Launch the CTS Scoreboard in Chromium kiosk mode on the local display.
# Local /web/... URLs are wrapped in the kiosk shell so the iframe can switch
# to /wifi/display while provisioning is active. External custom URLs keep the
# legacy direct-open behavior.

set -euo pipefail

URL="${CTS_KIOSK_URL:-/web/home}"
PROFILE_DIR="${CTS_KIOSK_PROFILE:-$HOME/.config/cts-kiosk-chromium}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_URL="${CTS_KIOSK_BASE_URL:-http://localhost:5000}"

mkdir -p "$PROFILE_DIR"

is_local_scoreboard_url() {
    case "$1" in
        /web/*) return 0 ;;
        http://localhost:5000/web/*) return 0 ;;
        http://127.0.0.1:5000/web/*) return 0 ;;
        *) return 1 ;;
    esac
}

open_url="$URL"
if is_local_scoreboard_url "$URL"; then
    display_url="$URL"
    if [[ "$display_url" != /web/* ]]; then
        display_url="$(python3 - "$display_url" <<'PYEOF'
import sys
from urllib.parse import urlsplit

parts = urlsplit(sys.argv[1])
path = parts.path or "/web/home"
if parts.query:
    path += "?" + parts.query
print(path)
PYEOF
)"
    fi
    open_url="${BASE_URL%/}/web/kiosk?$(python3 - "$display_url" <<'PYEOF'
import sys
from urllib.parse import urlencode, quote

print(urlencode({"display": sys.argv[1]}, quote_via=quote))
PYEOF
)"
    "$SCRIPT_DIR/wait-for-server.sh" "${BASE_URL%/}/web/kiosk" 60 || true
elif [[ "$URL" == http://* || "$URL" == https://* ]]; then
    open_url="$URL"
else
    open_url="${BASE_URL%/}$URL"
    "$SCRIPT_DIR/wait-for-server.sh" "$open_url" 60 || true
fi

# Suppress the "session ended badly" infobar that otherwise appears after
# a hard reboot or power loss.
PREFS="$PROFILE_DIR/Default/Preferences"
if [ -f "$PREFS" ]; then
    sed -i \
        -e 's/"exited_cleanly":false/"exited_cleanly":true/' \
        -e 's/"exit_type":"Crashed"/"exit_type":"Normal"/' \
        "$PREFS" || true
fi

# Pick the chromium binary.
CHROMIUM="$(command -v chromium || command -v chromium-browser || true)"
if [ -z "$CHROMIUM" ]; then
    echo "cts-kiosk: chromium is not installed. Run pi/scripts/install-kiosk.sh." >&2
    exit 1
fi

exec "$CHROMIUM" \
    --kiosk \
    --noerrdialogs \
    --disable-infobars \
    --disable-translate \
    --disable-features=TranslateUI \
    --disable-session-crashed-bubble \
    --disable-pinch \
    --overscroll-history-navigation=0 \
    --no-first-run \
    --check-for-update-interval=31536000 \
    --ozone-platform=wayland \
    --password-store=basic \
    --use-mock-keychain \
    --user-data-dir="$PROFILE_DIR" \
    --app="$open_url"
