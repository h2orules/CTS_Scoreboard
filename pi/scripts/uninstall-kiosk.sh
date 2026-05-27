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

step() { printf '\n==> %s\n' "$*"; }
log() { printf '  %s\n' "$*"; }

step "Stop and disable user service"
systemctl --user disable --now cts-scoreboard.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/cts-scoreboard.service"
systemctl --user daemon-reload || true

step "Strip managed block from labwc autostart"
AUTOSTART="$HOME/.config/labwc/autostart"
if [ -f "$AUTOSTART" ]; then
    tmp="$(mktemp)"
    awk -v b="$MARKER_BEGIN" -v e="$MARKER_END" '
        $0==b {skip=1; next}
        $0==e {skip=0; next}
        !skip {print}
    ' "$AUTOSTART" > "$tmp"
    mv "$tmp" "$AUTOSTART"
fi

step "Strip managed keybinds from rc.xml"
RC_XML="$HOME/.config/labwc/rc.xml"
if [ -f "$RC_XML" ]; then
    tmp="$(mktemp)"
    awk -v b="$XML_MARKER_BEGIN" -v e="$XML_MARKER_END" '
        index($0,b){skip=1; next}
        index($0,e){skip=0; next}
        !skip {print}
    ' "$RC_XML" > "$tmp"
    mv "$tmp" "$RC_XML"
fi

step "Remove desktop launchers"
rm -f "$HOME/Desktop/cts-kiosk.desktop"
rm -f "$HOME/.local/share/applications/cts-kiosk.desktop"

step "Done"
log "Lingering ('loginctl enable-linger') and screen-blanking settings were"
log "left as-is. Disable lingering with:  sudo loginctl disable-linger \$USER"
