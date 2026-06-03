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
if command -v chromium >/dev/null || command -v chromium-browser >/dev/null; then
    log "Chromium already installed."
elif (( DO_APT )); then
    run "sudo apt update"
    run "sudo apt install -y chromium"
else
    log "Chromium missing and --no-apt was passed. Install it manually."
fi

# ---------------------------------------------------------------------------
step "Make repo scripts executable"
run "chmod +x '$PI_DIR/scripts/cts-kiosk.sh' '$PI_DIR/scripts/cts-settings.sh' '$PI_DIR/scripts/wait-for-server.sh' '$PI_DIR/scripts/install-kiosk.sh' '$PI_DIR/scripts/uninstall-kiosk.sh'"

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
if (( DRY_RUN )); then
    log "[dry-run] would inject managed keybind block into $RC_XML"
else
    snippet="$(sed "s|SCOREBOARD_REPO|$REPO_DIR|g" "$PI_DIR/labwc/rc.xml.snippet")"
    # Use Python so we get correct XML placement (the previous awk-based
    # approach could leave our <keyboard> block OUTSIDE the root element
    # when rc.xml had its root tags on a single line, which silently
    # disables every keybind we install).
    SNIPPET_FILE="$(mktemp)"
    printf '%s\n' "$snippet" > "$SNIPPET_FILE"
    RC_XML="$RC_XML" SNIPPET_FILE="$SNIPPET_FILE" \
        BEGIN_MARK="$XML_MARKER_BEGIN" END_MARK="$XML_MARKER_END" \
        python3 - <<'PYEOF'
import os
import re
from pathlib import Path

rc = Path(os.environ["RC_XML"])
begin = os.environ["BEGIN_MARK"]
end = os.environ["END_MARK"]
snippet = Path(os.environ["SNIPPET_FILE"]).read_text()

text = rc.read_text() if rc.exists() else ""

# 1. Strip any previous managed block, regardless of where it sits.
managed_block_re = re.compile(
    re.escape(begin) + r".*?" + re.escape(end) + r"\n?",
    flags=re.DOTALL,
)
text = managed_block_re.sub("", text)

# 2. Strip any now-empty <keyboard></keyboard> wrapper left behind by
#    a previous (buggy) install that placed our block in its own
#    keyboard tag outside the root element.
text = re.sub(
    r"<keyboard>\s*</keyboard>\s*", "", text, flags=re.DOTALL
)

block = f"{begin}\n{snippet.rstrip()}\n{end}\n"

# 3. Decide where to insert.
#    Priority: inside an existing <keyboard> inside the root; else as a
#    new <keyboard> child of the root; else create a fresh labwc_config
#    root containing our keybinds.
def insert_before(haystack, needle_re, payload):
    m = needle_re.search(haystack)
    if not m:
        return None
    return haystack[: m.start()] + payload + haystack[m.start() :]

# Try inside an existing <keyboard>.
new_text = insert_before(text, re.compile(r"</keyboard>"), block)
if new_text is None:
    # Wrap our block in its own <keyboard> and insert before the
    # closing root tag (openbox_config or labwc_config).
    wrapped = f"<keyboard>\n{block}</keyboard>\n"
    root_close_re = re.compile(r"</(openbox_config|labwc_config)>")
    new_text = insert_before(text, root_close_re, wrapped)
if new_text is None:
    # No root present at all — write a complete minimal labwc_config.
    new_text = (
        '<?xml version="1.0"?>\n'
        "<labwc_config>\n"
        f"<keyboard>\n{block}</keyboard>\n"
        "</labwc_config>\n"
    )

rc.parent.mkdir(parents=True, exist_ok=True)
rc.write_text(new_text)
PYEOF
    rm -f "$SNIPPET_FILE"
fi

# ---------------------------------------------------------------------------
step "Install desktop launchers (Kiosk + Settings)"
DESKTOP_NAMES=("cts-kiosk.desktop" "cts-settings.desktop")
for desktop_name in "${DESKTOP_NAMES[@]}"; do
    source_desktop="$PI_DIR/desktop/$desktop_name"
    if [ ! -f "$source_desktop" ]; then
        log "Skipping $desktop_name (not found in repo)."
        continue
    fi
    for target in "$HOME/Desktop/$desktop_name" "$HOME/.local/share/applications/$desktop_name"; do
        run "mkdir -p '$(dirname "$target")'"
        if (( DRY_RUN )); then
            log "[dry-run] would write $target"
        else
            sed "s|SCOREBOARD_REPO|$REPO_DIR|g" "$source_desktop" > "$target"
            chmod +x "$target"
            # Mark trusted so file-manager double-click works without prompt.
            gio set "$target" metadata::trusted true 2>/dev/null || true
        fi
    done
done

# ---------------------------------------------------------------------------
step "Install launcher icons into hicolor icon theme"
ICON_DEST_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
ICON_NAMES=("cts-kiosk.svg" "cts-settings.svg")
installed_any_icon=0
for icon_name in "${ICON_NAMES[@]}"; do
    icon_src="$PI_DIR/desktop/$icon_name"
    icon_dest="$ICON_DEST_DIR/$icon_name"
    if [ ! -f "$icon_src" ]; then
        log "Note: $icon_src not found; corresponding .desktop will fall back to a generic icon."
        continue
    fi
    if (( DRY_RUN )); then
        log "[dry-run] would install $icon_src -> $icon_dest"
    else
        mkdir -p "$ICON_DEST_DIR"
        install -m 0644 "$icon_src" "$icon_dest"
        installed_any_icon=1
    fi
done
if (( installed_any_icon )) && command -v gtk-update-icon-cache >/dev/null; then
    gtk-update-icon-cache -q -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
step "Install 'cts-kiosk' / 'cts-settings' commands in /usr/local/bin"
# Exposing the launchers as real executables on $PATH lets the .desktop
# files use Exec=cts-kiosk / Exec=cts-settings, which libfm/pcmanfm
# treats as a normal application launch -- no "execute this script?"
# prompt, and no need to flip the global libfm quick_exec setting.
#
# We install into /usr/local/bin (not ~/.local/bin) because the labwc
# Wayland session does NOT source ~/.profile, so ~/.local/bin is not on
# the session PATH; libfm then fails to resolve Exec=cts-kiosk and
# falls back to the "execute this script?" prompt. /usr/local/bin is
# always on PATH for graphical sessions on Bookworm.
BIN_DIR="/usr/local/bin"
declare -a BIN_PAIRS=(
    "cts-kiosk:$PI_DIR/scripts/cts-kiosk.sh"
    "cts-settings:$PI_DIR/scripts/cts-settings.sh"
)
for pair in "${BIN_PAIRS[@]}"; do
    bin_name="${pair%%:*}"
    bin_src="${pair#*:}"
    bin_target="$BIN_DIR/$bin_name"
    if (( DRY_RUN )); then
        log "[dry-run] would symlink $bin_target -> $bin_src (via sudo)"
    else
        sudo ln -sfn "$bin_src" "$bin_target"
    fi
done
# Clean up legacy ~/.local/bin symlinks from previous installer versions
# so there's only one source of truth on $PATH.
for stale in "$HOME/.local/bin/cts-kiosk" "$HOME/.local/bin/cts-settings"; do
    if [ -L "$stale" ]; then
        if (( DRY_RUN )); then
            log "[dry-run] would remove legacy symlink $stale"
        else
            rm -f "$stale"
        fi
    fi
done
if ! command -v cts-kiosk >/dev/null; then
    log "Note: cts-kiosk not yet on \$PATH for this shell; should be available next login."
fi

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
