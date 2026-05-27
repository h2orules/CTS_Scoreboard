#!/usr/bin/env bash
# Launch the CTS Scoreboard in Chromium kiosk mode on the local display.
#
# - Waits for the gunicorn server to respond before opening the browser
#   (avoids a "site can't be reached" flash at boot).
# - Uses a dedicated Chromium profile so kiosk state never collides with
#   any normal browsing the operator does on the desktop.
# - Disables the "Restore pages?" bubble after an unclean shutdown.
#
# This script is invoked automatically by labwc autostart at login, and is
# also wired to a desktop launcher icon (~/Desktop/cts-kiosk.desktop) so
# the operator can re-enter kiosk mode after exiting to the desktop.

set -euo pipefail

URL="${CTS_KIOSK_URL:-http://localhost:5000/web/home}"
PROFILE_DIR="${CTS_KIOSK_PROFILE:-$HOME/.config/cts-kiosk-chromium}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$PROFILE_DIR"

# Wait for the local server (best-effort; continue even if it times out so
# Chromium still opens and shows its own error page rather than nothing).
"$SCRIPT_DIR/wait-for-server.sh" "${URL}" 60 || true

# Suppress the "session ended badly" infobar that otherwise appears after
# a hard reboot or power loss.
PREFS="$PROFILE_DIR/Default/Preferences"
if [ -f "$PREFS" ]; then
    sed -i \
        -e 's/"exited_cleanly":false/"exited_cleanly":true/' \
        -e 's/"exit_type":"Crashed"/"exit_type":"Normal"/' \
        "$PREFS" || true
fi

# Pick the chromium binary. Bookworm ships /usr/bin/chromium-browser as a
# wrapper around /usr/bin/chromium; either works.
CHROMIUM="$(command -v chromium-browser || command -v chromium || true)"
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
    --user-data-dir="$PROFILE_DIR" \
    --app="$URL"
