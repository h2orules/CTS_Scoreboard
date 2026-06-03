#!/usr/bin/env bash
# Install the CTS Scoreboard kiosk on this user's Raspberry Pi OS Bookworm
# (labwc/Wayland) account. Idempotent: re-running upgrades the install in
# place. Does not require root for the per-user pieces; will call sudo for
# the optional apt install / raspi-config tweaks.
#
#   pi/scripts/install-kiosk.sh                # full install
#   pi/scripts/install-kiosk.sh --dry-run      # show what would change
#   pi/scripts/install-kiosk.sh --no-apt       # skip Chromium apt install
#   pi/scripts/install-kiosk.sh --no-blanking  # don't touch screen-blanking
#   pi/scripts/install-kiosk.sh --no-linger    # don't enable user lingering
#
# After installation:
#   - reboot, or `systemctl --user start cts-scoreboard.service` to start
#     the server, then run `pi/scripts/cts-kiosk.sh` to test the browser.
#   - exit kiosk with Ctrl+Alt+K (closes Chromium, leaves the desktop).
#   - Ctrl+Alt+F2 is a TTY fallback if the keybind ever stops working.

set -euo pipefail

DRY_RUN=0
DO_APT=1
DO_BLANKING=1
DO_LINGER=1

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --no-apt) DO_APT=0 ;;
        --no-blanking) DO_BLANKING=0 ;;
        --no-linger) DO_LINGER=0 ;;
        -h|--help)
            sed -n '2,18p' "$0"; exit 0 ;;
        *)
            echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

MARKER_BEGIN="# >>> cts-scoreboard kiosk (managed) >>>"
MARKER_END="# <<< cts-scoreboard kiosk (managed) <<<"
XML_MARKER_BEGIN="<!-- >>> cts-scoreboard kiosk (managed) >>> -->"
XML_MARKER_END="<!-- <<< cts-scoreboard kiosk (managed) <<< -->"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
PI_DIR="$REPO_DIR/pi"

log() { printf '  %s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
run() {
    if (( DRY_RUN )); then
        printf '   [dry-run] %s\n' "$*"
    else
        eval "$@"
    fi
}

# ---------------------------------------------------------------------------
step "Sanity checks"
log "Repo:  $REPO_DIR"
log "User:  $USER"
log "HOME:  $HOME"

if [ ! -x "$REPO_DIR/start.sh" ]; then
    echo "ERROR: $REPO_DIR/start.sh not found or not executable." >&2
    exit 1
fi

if [ "${XDG_SESSION_TYPE:-}" != "wayland" ]; then
    log "Note: \$XDG_SESSION_TYPE is '${XDG_SESSION_TYPE:-unset}', not 'wayland'."
    log "      This installer targets the labwc Wayland session on Bookworm."
    log "      You can still proceed, but verify after reboot."
fi

# ---------------------------------------------------------------------------
step "Ensure Chromium is installed"
if command -v chromium-browser >/dev/null || command -v chromium >/dev/null; then
    log "Chromium already installed."
elif (( DO_APT )); then
    run "sudo apt update"
    run "sudo apt install -y chromium-browser"
else
    log "Chromium missing and --no-apt was passed. Install it manually."
fi

# ---------------------------------------------------------------------------
step "Make repo scripts executable"
run "chmod +x '$PI_DIR/scripts/cts-kiosk.sh' '$PI_DIR/scripts/wait-for-server.sh' '$PI_DIR/scripts/install-kiosk.sh' '$PI_DIR/scripts/uninstall-kiosk.sh'"

# ---------------------------------------------------------------------------
step "Install systemd --user service"
USER_UNIT_DIR="$HOME/.config/systemd/user"
run "mkdir -p '$USER_UNIT_DIR'"
run "install -m 0644 '$PI_DIR/systemd/cts-scoreboard.service' '$USER_UNIT_DIR/cts-scoreboard.service'"
run "systemctl --user daemon-reload"
run "systemctl --user enable --now cts-scoreboard.service"

if (( DO_LINGER )); then
    step "Enable user lingering (server starts even before desktop login)"
    run "sudo loginctl enable-linger '$USER'"
fi

# ---------------------------------------------------------------------------
step "Wire labwc autostart"
LABWC_DIR="$HOME/.config/labwc"
AUTOSTART="$LABWC_DIR/autostart"
run "mkdir -p '$LABWC_DIR'"
# NOTE: we deliberately do NOT copy /etc/xdg/labwc/autostart into the user's
# home. On Raspberry Pi OS Bookworm that file launches wf-panel-pi and
# pcmanfm-desktop; having both the system and a user copy results in two
# panels / two desktops. labwc still executes the system autostart when no
# user autostart exists, and on Pi's labwc build it also merges in the user
# autostart for additions, so we only need to maintain our managed block.
if (( DRY_RUN )); then
    log "[dry-run] would write managed block to $AUTOSTART"
else
    touch "$AUTOSTART"
    # Strip any prior managed block (and any lines the older installer
    # copied verbatim from /etc/xdg/labwc/autostart, which is what caused
    # the duplicate-panel bug), then append the fresh managed block.
    tmp="$(mktemp)"
    awk -v b="$MARKER_BEGIN" -v e="$MARKER_END" '
        $0==b {skip=1; next}
        $0==e {skip=0; next}
        !skip {print}
    ' "$AUTOSTART" > "$tmp"
    # Drop any lines that look like they were seeded from the system file
    # (wf-panel-pi, pcmanfm desktop, lxsession, kanshi, etc.). Keep blank
    # lines and any other user customisations.
    cleaned="$(mktemp)"
    grep -Ev '(wf-panel-pi|pcmanfm.*--desktop|lxsession|lxpolkit|^kanshi( |$)|lwrespawn)' "$tmp" > "$cleaned" || true
    {
        cat "$cleaned"
        printf '%s\n' "$MARKER_BEGIN"
        sed "s|SCOREBOARD_REPO|$REPO_DIR|g" "$PI_DIR/labwc/autostart"
        printf '%s\n' "$MARKER_END"
    } > "$AUTOSTART"
    rm -f "$tmp" "$cleaned"
fi

# ---------------------------------------------------------------------------
step "Wire labwc keybinds (Ctrl+Alt+K / Ctrl+Alt+S / Ctrl+Alt+R)"
RC_XML="$LABWC_DIR/rc.xml"
if [ ! -f "$RC_XML" ] && [ -f /etc/xdg/labwc/rc.xml ]; then
    run "cp /etc/xdg/labwc/rc.xml '$RC_XML'"
fi
if [ ! -f "$RC_XML" ]; then
    log "Note: $RC_XML not present and no /etc/xdg/labwc/rc.xml to seed from."
    log "      Skipping keybind install. You can still exit kiosk via"
    log "      Ctrl+Alt+F2 (TTY) or by killing chromium over SSH."
elif (( DRY_RUN )); then
    log "[dry-run] would inject managed keybind block into $RC_XML"
else
    # Remove any previous managed block, then inject before </keyboard> if
    # present, otherwise before </openbox_config> / </labwc_config>.
    tmp="$(mktemp)"
    awk -v b="$XML_MARKER_BEGIN" -v e="$XML_MARKER_END" '
        index($0,b){skip=1; next}
        index($0,e){skip=0; next}
        !skip {print}
    ' "$RC_XML" > "$tmp"

    snippet="$(sed "s|SCOREBOARD_REPO|$REPO_DIR|g" "$PI_DIR/labwc/rc.xml.snippet")"
    block="$(printf '%s\n%s\n%s\n' "$XML_MARKER_BEGIN" "$snippet" "$XML_MARKER_END")"

    if grep -q '</keyboard>' "$tmp"; then
        awk -v block="$block" '
            /<\/keyboard>/ && !done { print block; done=1 }
            { print }
        ' "$tmp" > "$RC_XML"
    else
        # No <keyboard> wrapper; create one before the closing root tag.
        wrapped="<keyboard>
$block
</keyboard>"
        awk -v block="$wrapped" '
            /<\/(openbox_config|labwc_config)>/ && !done { print block; done=1 }
            { print }
        ' "$tmp" > "$RC_XML"
        # If neither closing tag exists, append the block.
        if ! grep -q "$XML_MARKER_BEGIN" "$RC_XML"; then
            printf '\n%s\n' "$wrapped" >> "$RC_XML"
        fi
    fi
    rm -f "$tmp"
fi

# ---------------------------------------------------------------------------
step "Install desktop launcher icon"
DESKTOP_NAME="cts-kiosk.desktop"
DESKTOP_TARGETS=("$HOME/Desktop/$DESKTOP_NAME" "$HOME/.local/share/applications/$DESKTOP_NAME")
SOURCE_DESKTOP="$PI_DIR/desktop/$DESKTOP_NAME"

for target in "${DESKTOP_TARGETS[@]}"; do
    run "mkdir -p '$(dirname "$target")'"
    if (( DRY_RUN )); then
        log "[dry-run] would write $target"
    else
        sed "s|SCOREBOARD_REPO|$REPO_DIR|g" "$SOURCE_DESKTOP" > "$target"
        chmod +x "$target"
        # Mark trusted so file-manager double-click works without prompt.
        gio set "$target" metadata::trusted true 2>/dev/null || true
    fi
done

# ---------------------------------------------------------------------------
if (( DO_BLANKING )); then
    step "Disable console/X screen blanking via raspi-config"
    if command -v raspi-config >/dev/null; then
        # do_blanking 1 => disabled (yes, 1 disables; see raspi-config source).
        run "sudo raspi-config nonint do_blanking 1"
    else
        log "raspi-config not found; skipping screen-blanking change."
    fi
fi

# ---------------------------------------------------------------------------
step "Auto-login check (informational)"
if command -v raspi-config >/dev/null; then
    if sudo raspi-config nonint get_autologin 2>/dev/null | grep -q '^0$'; then
        log "Desktop auto-login appears enabled."
    else
        log "Desktop auto-login does NOT appear enabled."
        log "Run:  sudo raspi-config   ->  System Options  ->  Boot / Auto Login"
        log "      ->  Desktop Autologin"
    fi
fi

step "Done"
cat <<EOF

  Server:   systemctl --user status cts-scoreboard
  URL:      http://localhost:5000/web/home
  Re-enter kiosk: double-click "CTS Scoreboard Kiosk" on the desktop,
                  or press Ctrl+Alt+R, or run pi/scripts/cts-kiosk.sh.
  Exit kiosk:     Ctrl+Alt+K  (fallback: Ctrl+Alt+F2 -> pkill -f chromium)
  Settings:       Ctrl+Alt+S  (opens http://localhost:5000/settings)

  See docs/PI_KIOSK_SETUP.md for full details and troubleshooting.
EOF
