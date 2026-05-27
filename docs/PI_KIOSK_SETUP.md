# Raspberry Pi Kiosk Mode Setup

This guide configures a Raspberry Pi 5 running **Raspberry Pi OS Bookworm
(labwc / Wayland)** to boot directly into the CTS Scoreboard in fullscreen
Chromium kiosk mode on the first HDMI output.

At a glance:

| Component                | What it does                                              |
| ------------------------ | --------------------------------------------------------- |
| `cts-scoreboard.service` | `systemd --user` unit that runs `start.sh` (gunicorn).    |
| `pi/scripts/cts-kiosk.sh`| Waits for the server, then launches Chromium `--kiosk`.   |
| labwc `autostart`        | Runs `cts-kiosk.sh` at desktop login.                     |
| labwc `rc.xml`           | Keybinds: exit kiosk, open settings, re-enter kiosk.      |
| `cts-kiosk.desktop`      | Desktop icon to re-launch kiosk from the file manager.    |

URL the kiosk loads: **`http://localhost:5000/web/home`**

---

## Prerequisites

1. Raspberry Pi 5 with **Raspberry Pi OS Bookworm**, desktop edition.
2. Logged in as a regular user (commonly `pi`) that owns this repo clone at
   `~/scoreboard`.
3. The repo is installed and tested:

   ```bash
   cd ~/scoreboard
   uv sync
   uv run cts-scoreboard --help
   ```

### Verify labwc / Wayland

```bash
echo "$XDG_SESSION_TYPE"      # should print: wayland
pgrep -a labwc                # should show the labwc process
```

If it prints `x11` you are on the older Wayfire/X11 session. The installer
will warn but still proceed; the kiosk script falls back gracefully because
Chromium honors `--ozone-platform=wayland` only when Wayland is available.

### Verify (or enable) desktop auto-login

```bash
sudo raspi-config
#  -> 1 System Options
#     -> S5 Boot / Auto Login
#        -> B4 Desktop Autologin
```

Or check non-interactively:

```bash
sudo raspi-config nonint get_autologin   # 0 = enabled, 1 = disabled
```

Without auto-login the user service still works (with lingering), but
Chromium can't open until someone logs in at the console.

---

## Install

```bash
cd ~/scoreboard
./pi/scripts/install-kiosk.sh
```

What the installer does (all idempotent):

1. `apt install -y chromium-browser` (skippable with `--no-apt`).
2. Installs `~/.config/systemd/user/cts-scoreboard.service` and runs
   `systemctl --user enable --now`.
3. `sudo loginctl enable-linger $USER` so the server can start at boot,
   before anyone logs in (skippable with `--no-linger`).
4. Appends a managed block to `~/.config/labwc/autostart` that launches
   `pi/scripts/cts-kiosk.sh` at desktop login.
5. Injects three managed keybinds into `~/.config/labwc/rc.xml`:
   - **`Ctrl+Alt+K`** — exit kiosk (closes Chromium).
   - **`Ctrl+Alt+S`** — open the Settings page in a normal Chromium window.
   - **`Ctrl+Alt+R`** — re-enter kiosk mode.
6. Drops `cts-kiosk.desktop` on `~/Desktop/` and into
   `~/.local/share/applications/`, marking it trusted.
7. `sudo raspi-config nonint do_blanking 1` to disable screen blanking
   (skippable with `--no-blanking`).

Dry run first if you want to preview the changes:

```bash
./pi/scripts/install-kiosk.sh --dry-run
```

Reboot to verify the full boot flow:

```bash
sudo reboot
```

---

## What happens at boot

1. The Pi powers up and auto-logs into the labwc desktop session.
2. `systemd --user` starts `cts-scoreboard.service`, which runs
   [start.sh](../start.sh) → gunicorn + gevent on `0.0.0.0:5000`. With
   lingering enabled this may already be running before login.
3. labwc reads `~/.config/labwc/autostart` and executes
   `pi/scripts/cts-kiosk.sh`.
4. The kiosk script polls `http://127.0.0.1:5000/web/home` until it
   responds (up to 60 s), then launches Chromium in `--kiosk` mode with a
   dedicated profile at `~/.config/cts-kiosk-chromium/`.
5. Chromium fills HDMI-1 and shows the scoreboard.

---

## Exiting kiosk mode

You have two ways out:

### Primary: keyboard shortcut

Press **`Ctrl+Alt+K`**.

This is a labwc keybind that runs `pkill -INT chromium`. Chromium closes
and the labwc desktop appears. From there you can:

- Click the Wi-Fi/network icon in the system tray to join Wi-Fi.
- Press **`Ctrl+Alt+S`** to open the scoreboard `/settings` page in a
  regular Chromium window.
- Open a terminal, file manager, or any normal desktop app.

### Fallback: switch to a TTY

If the keybind ever fails (e.g. someone removed it from `rc.xml`):

1. Press **`Ctrl+Alt+F2`** — switches to a text console.
2. Log in with your normal Pi username/password.
3. `pkill -f chromium` (or `pkill -f cts-kiosk-chromium` to target only
   the kiosk profile).
4. Press **`Ctrl+Alt+F7`** (sometimes `F1`) to return to the desktop.

### Remote (SSH)

```bash
ssh pi@<address>
pkill -f cts-kiosk-chromium
```

---

## Re-entering kiosk mode

Any of these:

- **Double-click** the **CTS Scoreboard Kiosk** icon on the desktop.
- Press **`Ctrl+Alt+R`** on the keyboard.
- Run `~/scoreboard/pi/scripts/cts-kiosk.sh` from a terminal.

You do **not** need to restart the server; only Chromium is being
re-launched.

---

## Choosing the HDMI output

By default Chromium opens fullscreen on the **primary output**. On Pi 5
that is normally `HDMI-A-1` (the connector closer to the USB-C power port).

If you have two displays and want to force the primary:

```bash
# List outputs
wlr-randr

# Mark HDMI-A-1 as the primary and place it at 0,0
wlr-randr --output HDMI-A-1 --pos 0,0
```

To make this persist across reboots, add the `wlr-randr` line near the top
of `~/.config/labwc/autostart` (above the managed `cts-kiosk.sh` line).

---

## LAN access to the settings page

By default the server binds to `0.0.0.0:5000`, so any device on the same
network can visit `http://<pi-ip>:5000/settings` to load HyTek files,
configure Wi-Fi, etc. If you'd rather restrict access to the Pi itself,
edit the unit:

```bash
systemctl --user edit cts-scoreboard.service
# Add:
#   [Service]
#   Environment=BIND=127.0.0.1:5000
systemctl --user restart cts-scoreboard.service
```

---

## Troubleshooting

```bash
# Is the server up?
systemctl --user status cts-scoreboard
journalctl --user -u cts-scoreboard -f

# Test the URL the kiosk uses
curl -I http://localhost:5000/web/home

# Manually launch the kiosk script and watch its output
~/scoreboard/pi/scripts/cts-kiosk.sh

# Is the keybind installed?
grep -A2 'cts-scoreboard kiosk' ~/.config/labwc/rc.xml

# Reload labwc config without rebooting
labwc --reconfigure 2>/dev/null || pkill -HUP labwc
```

Common issues:

- **Black screen at boot, no Chromium** — auto-login isn't enabled, or
  labwc didn't read the autostart. Confirm with
  `cat ~/.config/labwc/autostart` and `pgrep -a chromium`.
- **"Restore pages?" bubble** — the kiosk script already strips this from
  the dedicated profile. If you see it once after a hard reboot, the next
  launch will clear it.
- **Screen turns black after a few minutes** — re-run the installer
  without `--no-blanking`, or `sudo raspi-config nonint do_blanking 1`.
- **Keybind does nothing** — open a terminal and run
  `labwc --reconfigure`, or log out and back in.

---

## Uninstall

```bash
~/scoreboard/pi/scripts/uninstall-kiosk.sh
```

Removes the user service, the managed blocks in `autostart` and `rc.xml`,
and the two desktop launchers. Chromium, lingering, and screen-blanking
settings are left in place; the uninstaller prints how to revert them.
