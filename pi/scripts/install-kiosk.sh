#!/usr/bin/env bash
# Install the CTS Scoreboard kiosk on this user's Raspberry Pi OS Bookworm
# (labwc/Wayland) account. Idempotent: re-running upgrades the install in
# place. Does not require root for the per-user pieces; will call sudo for
# the optional apt install / raspi-config tweaks.
#
#   pi/scripts/install-kiosk.sh                # full install
#   pi/scripts/install-kiosk.sh --dry-run      # show what would change
#   pi/scripts/install-kiosk.sh --no-wifi      # skip Wi-Fi provisioning setup
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
DO_WIFI=1

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --no-wifi) DO_WIFI=0 ;;
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

SYSTEMCTL="${SYSTEMCTL:-systemctl}"
SUDO="${SUDO:-sudo}"
APT="${APT:-apt}"
LOGINCTL="${LOGINCTL:-loginctl}"
GIO="${GIO:-gio}"
RASPI_CONFIG="${RASPI_CONFIG:-raspi-config}"
GTK_UPDATE_ICON_CACHE="${GTK_UPDATE_ICON_CACHE:-gtk-update-icon-cache}"
PYTHON3="${PYTHON3:-python3}"
CONTROLLER_DIR="${CTS_CONTROLLER_DIR:-/usr/local/lib/cts-scoreboard}"
SYSTEM_SERVICE_DIR="${CTS_SYSTEMD_DIR:-/etc/systemd/system}"
USER_SYSTEMD_DIR="${CTS_USER_SYSTEMD_DIR:-$HOME/.config/systemd/user}"
LABWC_DIR="${CTS_LABWC_DIR:-$HOME/.config/labwc}"
DESKTOP_DIR="${CTS_DESKTOP_DIR:-$HOME/Desktop}"
APP_DIR="${CTS_APPLICATIONS_DIR:-$HOME/.local/share/applications}"
ICON_DIR="${CTS_ICON_DIR:-$HOME/.local/share/icons/hicolor/scalable/apps}"
BIN_DIR="${CTS_BIN_DIR:-/usr/local/bin}"
SB_GROUP="${CTS_SCOREBOARD_GROUP:-$(id -gn)}"
TEST_MODE="${CTS_INSTALL_TEST_MODE:-0}"

controller_sources=(
    "$REPO_DIR/wifi_provisioning.py"
    "$REPO_DIR/wifi_provisioning_client.py"
    "$REPO_DIR/wifi_manager.py"
)
root_controller_targets=(
    "$CONTROLLER_DIR/wifi_provisioning.py"
    "$CONTROLLER_DIR/wifi_provisioning_client.py"
    "$CONTROLLER_DIR/wifi_manager.py"
)
wifi_prereq_packages=(dnsmasq-base nftables iproute2 network-manager avahi-daemon)

log() { printf '  %s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
run() {
    if (( DRY_RUN )); then
        printf '   [dry-run] %s\n' "$*"
    else
        eval "$@"
    fi
}

require_absolute_path() {
    case "$1" in
        /*) return 0 ;;
        *)
            echo "ERROR: expected absolute path: $1" >&2
            exit 1
            ;;
    esac
}

ensure_safe_root_target() {
    local target="$1"
    if (( DRY_RUN )) || [ "$TEST_MODE" = 1 ]; then
        return 0
    fi
    require_absolute_path "$target"
    python3 - "$target" <<'PYEOF'
import stat
import sys
from pathlib import Path

target = Path(sys.argv[1])
for path in (target, *target.parents):
    try:
        info = path.lstat()
    except FileNotFoundError:
        continue
    if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        sys.exit(f"ERROR: root installation path is not trusted: {path}")
PYEOF
}

service_is_active() {
    local scope="$1" name="$2"
    if [ -n "$scope" ]; then
        "$SYSTEMCTL" "$scope" is-active --quiet "$name" 2>/dev/null
    else
        "$SYSTEMCTL" is-active --quiet "$name" 2>/dev/null
    fi
}

service_is_enabled() {
    local scope="$1" name="$2"
    if [ -n "$scope" ]; then
        "$SYSTEMCTL" "$scope" is-enabled --quiet "$name" 2>/dev/null
    else
        "$SYSTEMCTL" is-enabled --quiet "$name" 2>/dev/null
    fi
}

stop_service_if_active() {
    local scope="$1" name="$2"
    if (( DRY_RUN )); then
        log "[dry-run] would stop $name if active"
        return 0
    fi
    if service_is_active "$scope" "$name"; then
        if [ -n "$scope" ]; then
            run "'$SYSTEMCTL' '$scope' stop '$name'"
        else
            run "'$SUDO' '$SYSTEMCTL' stop '$name'"
            if [ "$name" = cts-wifi-provisioning.service ]; then
                result="$("$SYSTEMCTL" show -p Result --value "$name")"
                if [ "$result" != success ]; then
                    echo "ERROR: Wi-Fi controller cleanup failed ($result); inspect its journal before continuing." >&2
                    return 1
                fi
            fi
        fi
    fi
}

controller_sources_complete() {
    for source_path in "${controller_sources[@]}"; do
        [ -f "$source_path" ] || return 1
    done
    [ -f "$PI_DIR/systemd/cts-wifi-provisioning.service" ]
}

install_wifi_prereqs() {
    if ! (( controller_present )); then
        return 0
    fi
    if (( DO_APT )); then
        run "$SUDO $APT update"
        run "$SUDO $APT install -y ${wifi_prereq_packages[*]}"
    else
        missing=()
        for pkg in "${wifi_prereq_packages[@]}"; do
            case "$pkg" in
                dnsmasq-base) cmd="dnsmasq" ;;
                nftables) cmd="nft" ;;
                iproute2) cmd="ip" ;;
                network-manager) cmd="nmcli" ;;
                avahi-daemon) cmd="avahi-daemon" ;;
            esac
            if ! command -v "$cmd" >/dev/null 2>&1; then
                missing+=("$pkg")
            fi
        done
        if ((${#missing[@]} > 0)); then
            echo "ERROR: missing Wi-Fi provisioning prerequisites (${missing[*]})." >&2
            echo "Install them or rerun with apt enabled." >&2
            exit 1
        fi
    fi
}

stage_root_controller_update() {
    if ! (( controller_present )); then
        return 0
    fi
    ensure_safe_root_target "$CONTROLLER_DIR"
    ensure_safe_root_target "$SYSTEM_SERVICE_DIR"
    ensure_safe_root_target "$BIN_DIR"
    stop_service_if_active "" cts-wifi-provisioning.service || return 1
    return 0
}

install_root_controller() {
    if ! (( controller_present )); then
        return 0
    fi
    run "$SUDO mkdir -p '$CONTROLLER_DIR' '$SYSTEM_SERVICE_DIR'"
    for controller_file in "${controller_sources[@]}"; do
        source_path="$controller_file"
        target_path="$CONTROLLER_DIR/$(basename "$controller_file")"
        ensure_safe_root_target "$target_path"
        run "$SUDO install -m 0644 '$source_path' '$target_path'"
    done
    ensure_safe_root_target "$SYSTEM_SERVICE_DIR/cts-wifi-provisioning.service"
    service_src="$PI_DIR/systemd/cts-wifi-provisioning.service"
    service_tmp="$HOME/.config/cts-scoreboard/cts-wifi-provisioning.service.$$.$RANDOM"
    if (( DRY_RUN )); then
        log "[dry-run] would write root service $SYSTEM_SERVICE_DIR/cts-wifi-provisioning.service"
    else
        mkdir -p "$(dirname "$service_tmp")"
        python3 - "$service_src" "$service_tmp" "$SB_GROUP" <<'PYEOF'
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text()
source = source.replace("CTS_SCOREBOARD_GROUP", sys.argv[3])
Path(sys.argv[2]).write_text(source)
PYEOF
        run "$SUDO install -m 0644 '$service_tmp' '$SYSTEM_SERVICE_DIR/cts-wifi-provisioning.service'"
        rm -f "$service_tmp"
    fi
}

disable_wifi_integration() {
    stop_service_if_active "--user" cts-scoreboard.service || return 1
    run "rm -f '$USER_SYSTEMD_DIR/cts-scoreboard.service.d/10-wifi-provisioning.conf'"
    if service_is_active "" cts-wifi-provisioning.service || service_is_enabled "" cts-wifi-provisioning.service; then
        stop_service_if_active "" cts-wifi-provisioning.service || return 1
        run "$SUDO $SYSTEMCTL disable cts-wifi-provisioning.service"
    fi
    for target_path in "$SYSTEM_SERVICE_DIR/cts-wifi-provisioning.service" \
        "$BIN_DIR/cts-wifi-setup" "${root_controller_targets[@]}"; do
        ensure_safe_root_target "$target_path"
        run "$SUDO rm -f '$target_path'"
    done
    run "$SUDO $SYSTEMCTL daemon-reload"
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
step "Install Wi-Fi provisioning controller"
controller_present=0
if (( DO_WIFI )); then
    if ! controller_sources_complete; then
        echo "ERROR: Wi-Fi provisioning install requires wifi_provisioning.py, wifi_provisioning_client.py, wifi_manager.py, and pi/systemd/cts-wifi-provisioning.service." >&2
        exit 1
    fi
    controller_present=1
    install_wifi_prereqs
    run "$SUDO $SYSTEMCTL enable --now avahi-daemon.service"
    stage_root_controller_update
    install_root_controller
    if (( DRY_RUN )); then
        log "[dry-run] would enable cts-wifi-provisioning.service"
    else
        run "$SUDO $SYSTEMCTL daemon-reload"
        run "$SUDO $SYSTEMCTL enable --now cts-wifi-provisioning.service"
    fi
else
    disable_wifi_integration
    log "Wi-Fi provisioning skipped by --no-wifi."
fi

# ---------------------------------------------------------------------------
step "Ensure Chromium is installed"
if command -v chromium >/dev/null || command -v chromium-browser >/dev/null; then
    log "Chromium already installed."
elif (( DO_APT )); then
    run "$SUDO $APT update"
    run "$SUDO $APT install -y chromium"
else
    log "Chromium missing and --no-apt was passed. Install it manually."
fi

# ---------------------------------------------------------------------------
step "Make repo scripts executable"
run "chmod +x '$PI_DIR/scripts/cts-kiosk.sh' '$PI_DIR/scripts/cts-settings.sh' '$PI_DIR/scripts/cts-wifi-setup.sh' '$PI_DIR/scripts/wait-for-server.sh' '$PI_DIR/scripts/install-kiosk.sh' '$PI_DIR/scripts/uninstall-kiosk.sh'"

# ---------------------------------------------------------------------------
step "Install systemd --user service"
if service_is_active "--user" cts-scoreboard.service; then
    run "$SYSTEMCTL --user stop cts-scoreboard.service"
fi
run "mkdir -p '$USER_SYSTEMD_DIR'"
run "install -m 0644 '$PI_DIR/systemd/cts-scoreboard.service' '$USER_SYSTEMD_DIR/cts-scoreboard.service'"
DROPIN_DIR="$USER_SYSTEMD_DIR/cts-scoreboard.service.d"
DROPIN_FILE="$DROPIN_DIR/10-wifi-provisioning.conf"
if (( controller_present )); then
    if (( DRY_RUN )); then
        log "[dry-run] would write $DROPIN_FILE"
    else
        mkdir -p "$DROPIN_DIR"
        python3 - "$DROPIN_FILE" "$CONTROLLER_DIR" "$SYSTEM_SERVICE_DIR" "$SB_GROUP" <<'PYEOF'
from pathlib import Path
import sys

dropin = Path(sys.argv[1])
controller_dir = sys.argv[2]
service_dir = sys.argv[3]
group = sys.argv[4]
text = f"""[Unit]
After=
Wants=

[Service]
Environment=CTS_WIFI_PROVISIONING=1
Environment=CTS_WIFI_SOCKET=/run/cts-scoreboard-wifi/control.sock
Environment=CTS_WIFI_CONTROLLER_DIR={controller_dir}
Environment=CTS_WIFI_SYSTEM_SERVICE_DIR={service_dir}
Environment=CTS_WIFI_SOCKET_GROUP={group}
"""
dropin.write_text(text)
PYEOF
    fi
else
    if (( DRY_RUN )); then
        log "[dry-run] would remove $DROPIN_FILE"
    else
        rm -f "$DROPIN_FILE"
    fi
fi
run "$SYSTEMCTL --user daemon-reload"
run "$SYSTEMCTL --user enable --now cts-scoreboard.service"

if (( DO_WIFI && controller_present )); then
    step "Install Wi-Fi request helper command"
    if (( DRY_RUN )); then
        log "[dry-run] would install cts-wifi-setup into $BIN_DIR"
    else
        ensure_safe_root_target "$BIN_DIR/cts-wifi-setup"
        run "$SUDO install -m 0755 '$PI_DIR/scripts/cts-wifi-setup.sh' '$BIN_DIR/cts-wifi-setup'"
    fi
fi

if (( DO_LINGER )); then
    step "Enable user lingering (server starts even before desktop login)"
    run "$SUDO $LOGINCTL enable-linger '$USER'"
fi

# ---------------------------------------------------------------------------
step "Install sb-* shell aliases for managing the service"
# Convenience aliases for stopping the production server while debugging
# from VS Code, then restarting it. Written as a managed block in
# ~/.bashrc so re-running the installer keeps them in sync.
BASHRC="$HOME/.bashrc"
if (( DRY_RUN )); then
    log "[dry-run] would write managed sb-* alias block to $BASHRC"
else
    touch "$BASHRC"
    python3 - "$BASHRC" "$MARKER_BEGIN" "$MARKER_END" <<'PYEOF'
from pathlib import Path
import sys

path = Path(sys.argv[1])
begin = sys.argv[2]
end = sys.argv[3]
lines = path.read_text().splitlines()
result = []
skip = False
for line in lines:
    if line == begin:
        skip = True
        continue
    if line == end:
        skip = False
        continue
    if not skip:
        result.append(line)
text = "\n".join(result).rstrip("\n")
if text:
    text += "\n"
text += begin + "\n"
text += """# Manage the cts-scoreboard --user service (stop for VS Code debugging, etc.)
alias sb-stop='systemctl --user stop cts-scoreboard.service'
alias sb-start='systemctl --user start cts-scoreboard.service'
alias sb-enable='systemctl --user enable --now cts-scoreboard.service'
alias sb-disable='systemctl --user disable --now cts-scoreboard.service'
alias sb-status='systemctl --user status cts-scoreboard.service'
alias sb-log='journalctl --user -u cts-scoreboard.service -f'
"""
text += end + "\n"
path.write_text(text)
PYEOF
    log "sb-stop / sb-start / sb-enable / sb-disable / sb-status / sb-log installed."
    log "Open a new shell or run 'source ~/.bashrc' to pick them up."
fi

# ---------------------------------------------------------------------------
step "Wire labwc autostart"
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
    python3 - "$AUTOSTART" "$MARKER_BEGIN" "$MARKER_END" "$REPO_DIR" "$PI_DIR/labwc/autostart" <<'PYEOF'
from pathlib import Path
import re
import sys

autostart = Path(sys.argv[1])
begin = sys.argv[2]
end = sys.argv[3]
repo_dir = sys.argv[4]
snippet_path = Path(sys.argv[5])
text = autostart.read_text() if autostart.exists() else ""
text = re.sub(re.escape(begin) + r".*?" + re.escape(end) + r"\n?", "", text, flags=re.DOTALL)
text = re.sub(r"(?:^|\n)(?:wf-panel-pi|pcmanfm.*--desktop|lxsession|lxpolkit|kanshi(?:\s|$)|lwrespawn)[^\n]*", "\n", text)
text = text.strip("\n")
if text:
    text += "\n"
snippet = snippet_path.read_text().replace("SCOREBOARD_REPO", repo_dir).rstrip("\n")
text += begin + "\n" + snippet + "\n" + end + "\n"
autostart.write_text(text)
PYEOF
fi

# ---------------------------------------------------------------------------
step "Wire labwc keybinds (Ctrl+Alt+K / Ctrl+Alt+S / Ctrl+Alt+R / Ctrl+Alt+W)"
RC_XML="$LABWC_DIR/rc.xml"
if [ ! -f "$RC_XML" ] && [ -f /etc/xdg/labwc/rc.xml ]; then
    run "cp /etc/xdg/labwc/rc.xml '$RC_XML'"
fi
if (( DRY_RUN )); then
    log "[dry-run] would inject managed keybind block into $RC_XML"
else
    RC_XML="$RC_XML" REPO_DIR="$REPO_DIR" BEGIN_MARK="$XML_MARKER_BEGIN" END_MARK="$XML_MARKER_END" \
        python3 - "$PI_DIR/labwc/rc.xml.snippet" <<'PYEOF'
import os
import re
from pathlib import Path
import sys

rc = Path(os.environ["RC_XML"])
begin = os.environ["BEGIN_MARK"]
end = os.environ["END_MARK"]
snippet = Path(sys.argv[1]).read_text().replace("SCOREBOARD_REPO", os.environ["REPO_DIR"])

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
    for target in "$DESKTOP_DIR/$desktop_name" "$APP_DIR/$desktop_name"; do
        run "mkdir -p '$(dirname "$target")'"
        if (( DRY_RUN )); then
            log "[dry-run] would write $target"
        else
            sed "s|SCOREBOARD_REPO|$REPO_DIR|g" "$source_desktop" > "$target"
            chmod +x "$target"
            # Mark trusted so file-manager double-click works without prompt.
            "$GIO" set "$target" metadata::trusted true 2>/dev/null || true
        fi
    done
done

# ---------------------------------------------------------------------------
step "Install launcher icons into hicolor icon theme"
ICON_DEST_DIR="$ICON_DIR"
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
if (( installed_any_icon )) && command -v "$GTK_UPDATE_ICON_CACHE" >/dev/null; then
    "$GTK_UPDATE_ICON_CACHE" -q -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
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
for stale in "$HOME/.local/bin/cts-kiosk" "$HOME/.local/bin/cts-settings" "$HOME/.local/bin/cts-wifi-setup"; do
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
step "Silence libfm 'execute this script?' prompt for desktop launchers"
# Even with a valid Exec= on $PATH, x-bit set, and metadata::trusted=true,
# libfm on Bookworm still prompts when launching .desktop files from the
# desktop. The reliable knob is per-user quick_exec=1 in libfm.conf.
LIBFM_CONF_DIR="$HOME/.config/libfm"
LIBFM_CONF="$LIBFM_CONF_DIR/libfm.conf"
if (( DRY_RUN )); then
    log "[dry-run] would ensure quick_exec=1 in $LIBFM_CONF"
else
    mkdir -p "$LIBFM_CONF_DIR"
    if [ ! -f "$LIBFM_CONF" ]; then
        printf '[config]\nquick_exec=1\n' > "$LIBFM_CONF"
    elif ! grep -qE '^\s*quick_exec\s*=' "$LIBFM_CONF"; then
        if grep -qE '^\s*\[config\]' "$LIBFM_CONF"; then
            # Insert quick_exec=1 right after the [config] section header.
            python3 - "$LIBFM_CONF" <<'PYEOF'
import re, sys, pathlib
p = pathlib.Path(sys.argv[1])
text = p.read_text()
text = re.sub(r'(\[config\][^\n]*\n)', r'\1quick_exec=1\n', text, count=1)
p.write_text(text)
PYEOF
        else
            printf '\n[config]\nquick_exec=1\n' >> "$LIBFM_CONF"
        fi
    else
        sed -i -E 's/^\s*quick_exec\s*=.*/quick_exec=1/' "$LIBFM_CONF"
    fi
    log "quick_exec=1 set in $LIBFM_CONF"
fi

# ---------------------------------------------------------------------------
if (( DO_BLANKING )); then
    step "Disable console/X screen blanking via raspi-config"
    if command -v "$RASPI_CONFIG" >/dev/null; then
        # do_blanking 1 => disabled (yes, 1 disables; see raspi-config source).
        run "$SUDO $RASPI_CONFIG nonint do_blanking 1"
    else
        log "raspi-config not found; skipping screen-blanking change."
    fi
fi

# ---------------------------------------------------------------------------
step "Auto-login check (informational)"
if command -v "$RASPI_CONFIG" >/dev/null; then
    if $SUDO $RASPI_CONFIG nonint get_autologin 2>/dev/null | grep -q '^0$'; then
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
  Aliases:  sb-stop / sb-start / sb-enable / sb-disable / sb-status / sb-log
            (open a new shell or 'source ~/.bashrc' to use them)
  URL:      http://localhost:5000/web/home
  Re-enter kiosk: double-click "CTS Scoreboard Kiosk" on the desktop,
                  or press Ctrl+Alt+R, or run pi/scripts/cts-kiosk.sh.
  Exit kiosk:     Ctrl+Alt+K  (fallback: Ctrl+Alt+F2 -> pkill -f chromium)
  Settings:       Ctrl+Alt+S  (opens http://localhost:5000/settings)

  See docs/PI_KIOSK_SETUP.md for full details and troubleshooting.
EOF
