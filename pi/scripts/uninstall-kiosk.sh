#!/usr/bin/env bash
# Uninstall the CTS Scoreboard kiosk integration from this user's account.
# Removes the systemd --user service, labwc autostart/keybind blocks, and
# desktop launchers. Does not uninstall Chromium and does not touch the
# repo itself.

set -euo pipefail

MARKER_BEGIN="# >>> cts-scoreboard kiosk (managed) >>>"
MARKER_END="# <<< cts-scoreboard kiosk (managed) <<<"
XML_MARKER_BEGIN="<!-- >>> cts-scoreboard kiosk (managed) >>> -->"
XML_MARKER_END="<!-- <<< cts-scoreboard kiosk (managed) <<< -->"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
SUDO="${SUDO:-sudo}"
USER_SYSTEMD_DIR="${CTS_USER_SYSTEMD_DIR:-$HOME/.config/systemd/user}"
SYSTEM_SERVICE_DIR="${CTS_SYSTEMD_DIR:-/etc/systemd/system}"
LABWC_DIR="${CTS_LABWC_DIR:-$HOME/.config/labwc}"
DESKTOP_DIR="${CTS_DESKTOP_DIR:-$HOME/Desktop}"
APP_DIR="${CTS_APPLICATIONS_DIR:-$HOME/.local/share/applications}"
BIN_DIR="${CTS_BIN_DIR:-/usr/local/bin}"
CONTROLLER_DIR="${CTS_CONTROLLER_DIR:-/usr/local/lib/cts-scoreboard}"

step() { printf '\n==> %s\n' "$*"; }
log() { printf '  %s\n' "$*"; }

step "Stop and disable user service"
$SYSTEMCTL --user disable --now cts-scoreboard.service 2>/dev/null || true
rm -f "$USER_SYSTEMD_DIR/cts-scoreboard.service"
[ -d "$USER_SYSTEMD_DIR/cts-scoreboard.service.d" ] && rm -f "$USER_SYSTEMD_DIR/cts-scoreboard.service.d/10-wifi-provisioning.conf"
$SYSTEMCTL --user daemon-reload || true

step "Stop and disable Wi-Fi provisioning controller"
if $SYSTEMCTL show -p LoadState --value cts-wifi-provisioning.service 2>/dev/null | grep -qx loaded; then
    ${SUDO} ${SYSTEMCTL} disable --now cts-wifi-provisioning.service
    result="$("$SYSTEMCTL" show -p Result --value cts-wifi-provisioning.service)"
    if [ "$result" != success ]; then
        echo "ERROR: Wi-Fi controller cleanup failed ($result); keep its installed files and inspect the journal." >&2
        exit 1
    fi
fi
$SUDO rm -f "$SYSTEM_SERVICE_DIR/cts-wifi-provisioning.service"
$SUDO $SYSTEMCTL daemon-reload

step "Remove root-owned controller artifacts"
$SUDO rm -f "$CONTROLLER_DIR/wifi_provisioning.py" \
            "$CONTROLLER_DIR/wifi_provisioning_client.py" \
            "$CONTROLLER_DIR/wifi_manager.py"

step "Strip managed block from labwc autostart"
AUTOSTART="$LABWC_DIR/autostart"
if [ -f "$AUTOSTART" ]; then
    python3 - "$AUTOSTART" "$MARKER_BEGIN" "$MARKER_END" <<'PYEOF'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
begin = sys.argv[2]
end = sys.argv[3]
text = path.read_text()
text = re.sub(re.escape(begin) + r".*?" + re.escape(end) + r"\n?", "", text, flags=re.DOTALL)
path.write_text(text.strip("\n") + ("\n" if text.strip("\n") else ""))
PYEOF
fi

step "Strip managed keybinds from rc.xml"
RC_XML="$LABWC_DIR/rc.xml"
if [ -f "$RC_XML" ]; then
    python3 - "$RC_XML" "$XML_MARKER_BEGIN" "$XML_MARKER_END" <<'PYEOF'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
begin = sys.argv[2]
end = sys.argv[3]
text = path.read_text()
text = re.sub(re.escape(begin) + r".*?" + re.escape(end) + r"\n?", "", text, flags=re.DOTALL)
text = re.sub(r"<keyboard>\s*</keyboard>\s*", "", text, flags=re.DOTALL)
path.write_text(text.strip("\n") + ("\n" if text.strip("\n") else ""))
PYEOF
fi

step "Remove desktop launchers"
rm -f "$DESKTOP_DIR/cts-kiosk.desktop" "$APP_DIR/cts-kiosk.desktop"
rm -f "$DESKTOP_DIR/cts-settings.desktop" "$APP_DIR/cts-settings.desktop"
$SUDO rm -f "$BIN_DIR/cts-kiosk" "$BIN_DIR/cts-settings" "$BIN_DIR/cts-wifi-setup"

step "Done"
log "Lingering ('loginctl enable-linger') and screen-blanking settings were"
log "left as-is. Disable lingering with:  sudo loginctl disable-linger \$USER"
