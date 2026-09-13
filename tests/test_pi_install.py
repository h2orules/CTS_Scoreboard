from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def _make_fake_command(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body + "\n")
    path.chmod(0o755)


def _run(script: str, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(REPO / script), *args],
        cwd=str(REPO),
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )


def _run_allow_fail(script: str, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(REPO / script), *args],
        cwd=str(REPO),
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )


def test_install_and_uninstall_manage_provisioning_assets(tmp_path):
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    user_systemd = tmp_path / "user-systemd"
    labwc = tmp_path / "labwc"
    desktop = tmp_path / "desktop"
    apps = tmp_path / "apps"
    icons = tmp_path / "icons" / "hicolor" / "scalable" / "apps"
    bin_target = tmp_path / "usr-local-bin"
    controller_dir = tmp_path / "controller"
    system_service_dir = tmp_path / "systemd"
    log_file = tmp_path / "calls.log"

    for path in [home, bin_dir, user_systemd, labwc, desktop, apps, icons, bin_target, controller_dir, system_service_dir]:
        path.mkdir(parents=True, exist_ok=True)
    (home / ".bashrc").write_text("# existing\n")
    (labwc / "autostart").write_text("wf-panel-pi\n")
    (labwc / "rc.xml").write_text("<labwc_config><keyboard></keyboard></labwc_config>\n")
    (user_systemd / "cts-scoreboard.service.d").mkdir(parents=True, exist_ok=True)
    (user_systemd / "cts-scoreboard.service.d" / "10-wifi-provisioning.conf").write_text("existing\n")
    user_override = user_systemd / "cts-scoreboard.service.d" / "90-local.conf"
    user_override.write_text("[Service]\nEnvironment=LOCAL_SETTING=kept\n")
    (system_service_dir / "cts-wifi-provisioning.service").write_text("existing\n")
    (controller_dir / "wifi_provisioning.py").write_text("existing\n")
    (controller_dir / "wifi_provisioning_client.py").write_text("existing\n")
    (controller_dir / "wifi_manager.py").write_text("existing\n")
    (bin_target / "cts-wifi-setup").write_text("existing\n")
    (REPO / "pi" / "labwc" / "autostart").read_text()

    _make_fake_command(
        bin_dir,
        "sudo",
        "exec \"$@\"",
    )
    _make_fake_command(
        bin_dir,
        "systemctl",
        f'printf "systemctl %s\\n" "$*" >> "{log_file}"\n'
        'if [ "${1:-}" = show ]; then if [ "${3:-}" = Result ]; then echo success; else echo loaded; fi; fi\nexit 0',
    )
    _make_fake_command(
        bin_dir,
        "apt",
        f'printf "apt %s\\n" "$*" >> "{log_file}"\nexit 0',
    )
    _make_fake_command(
        bin_dir,
        "loginctl",
        f'printf "loginctl %s\\n" "$*" >> "{log_file}"\nexit 0',
    )
    _make_fake_command(
        bin_dir,
        "gio",
        f'printf "gio %s\\n" "$*" >> "{log_file}"\nexit 0',
    )
    _make_fake_command(
        bin_dir,
        "raspi-config",
        "if [ \"${2:-}\" = get_autologin ]; then echo 0; fi\nexit 0",
    )
    _make_fake_command(
        bin_dir,
        "gtk-update-icon-cache",
        "exit 0",
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "HOME": str(home),
            "XDG_SESSION_TYPE": "wayland",
            "CTS_USER_SYSTEMD_DIR": str(user_systemd),
            "CTS_LABWC_DIR": str(labwc),
            "CTS_DESKTOP_DIR": str(desktop),
            "CTS_APPLICATIONS_DIR": str(apps),
            "CTS_ICON_DIR": str(icons),
            "CTS_BIN_DIR": str(bin_target),
            "CTS_CONTROLLER_DIR": str(controller_dir),
            "CTS_SYSTEMD_DIR": str(system_service_dir),
            "CTS_SCOREBOARD_GROUP": "scoreboard",
            "CTS_INSTALL_TEST_MODE": "1",
        }
    )

    _run("pi/scripts/install-kiosk.sh", env)
    _run("pi/scripts/install-kiosk.sh", env)

    user_unit = user_systemd / "cts-scoreboard.service"
    dropin = user_systemd / "cts-scoreboard.service.d" / "10-wifi-provisioning.conf"
    root_unit = system_service_dir / "cts-wifi-provisioning.service"

    assert user_unit.exists()
    assert dropin.exists()
    assert "CTS_WIFI_PROVISIONING=1" in dropin.read_text()
    assert "After=" in dropin.read_text()
    assert root_unit.exists()
    assert "ExecStart=/usr/bin/python3 /usr/local/lib/cts-scoreboard/wifi_provisioning.py" in root_unit.read_text()
    assert (controller_dir / "wifi_provisioning.py").exists()
    assert (controller_dir / "wifi_provisioning_client.py").exists()
    assert (controller_dir / "wifi_manager.py").exists()
    assert "dnsmasq-base nftables iproute2 network-manager avahi-daemon" in log_file.read_text()
    assert "enable --now avahi-daemon.service" in log_file.read_text()
    assert "enable --now cts-wifi-provisioning.service" in log_file.read_text()
    assert (labwc / "autostart").read_text().count("# >>> cts-scoreboard kiosk (managed) >>>") == 1
    assert (labwc / "rc.xml").read_text().count("<!-- >>> cts-scoreboard kiosk (managed) >>> -->") == 1
    assert "C-A-w" in (labwc / "rc.xml").read_text()
    assert (desktop / "cts-kiosk.desktop").exists()
    assert (apps / "cts-kiosk.desktop").exists()
    assert (bin_target / "cts-wifi-setup").exists()
    assert "/usr/local/lib/cts-scoreboard/wifi_provisioning_client.py" in (bin_target / "cts-wifi-setup").read_text()

    snapshot = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*") if path.is_file() and path != log_file
    }
    for flags in [("--dry-run",), ("--dry-run", "--no-wifi")]:
        calls_before = len(log_file.read_text())
        _run("pi/scripts/install-kiosk.sh", env, *flags)
        assert {
            path.relative_to(tmp_path): path.read_bytes()
            for path in tmp_path.rglob("*") if path.is_file() and path != log_file
        } == snapshot
        dry_calls = log_file.read_text()[calls_before:]
        assert " stop " not in dry_calls
        assert " disable " not in dry_calls
        assert " enable " not in dry_calls

    _make_fake_command(
        bin_dir,
        "systemctl",
        f'printf "systemctl %s\\n" "$*" >> "{log_file}"\n'
        'if [ "${1:-}" = show ]; then if [ "${3:-}" = Result ]; then echo success; else echo loaded; fi; fi\n'
        'if [ "$*" = "disable --now cts-wifi-provisioning.service" ]; then exit 1; fi\n'
        'exit 0',
    )
    result = _run_allow_fail("pi/scripts/uninstall-kiosk.sh", env)
    assert result.returncode != 0
    assert root_unit.exists()
    assert (controller_dir / "wifi_provisioning.py").exists()
    assert (bin_target / "cts-wifi-setup").exists()

    _make_fake_command(
        bin_dir,
        "systemctl",
        'if [ "${1:-}" = show ]; then\n'
        '  if [ "${3:-}" = Result ]; then echo exit-code; else echo loaded; fi\n'
        'fi\nexit 0',
    )
    result = _run_allow_fail("pi/scripts/uninstall-kiosk.sh", env)
    assert result.returncode != 0
    assert "cleanup failed" in result.stderr
    assert root_unit.exists()
    assert (controller_dir / "wifi_provisioning.py").exists()

    _make_fake_command(
        bin_dir,
        "systemctl",
        f'printf "systemctl %s\\n" "$*" >> "{log_file}"\n'
        'if [ "${1:-}" = show ]; then if [ "${3:-}" = Result ]; then echo success; else echo loaded; fi; fi\nexit 0',
    )
    _run("pi/scripts/uninstall-kiosk.sh", env)
    assert "disable --now cts-wifi-provisioning.service" in log_file.read_text()

    assert not user_unit.exists()
    assert not dropin.exists()
    assert not root_unit.exists()
    assert not (controller_dir / "wifi_provisioning.py").exists()
    assert not (controller_dir / "wifi_provisioning_client.py").exists()
    assert not (controller_dir / "wifi_manager.py").exists()
    assert not (desktop / "cts-kiosk.desktop").exists()
    assert not (apps / "cts-kiosk.desktop").exists()
    assert not (bin_target / "cts-wifi-setup").exists()
    assert (user_systemd / "cts-scoreboard.service.d").exists()
    assert user_override.read_text() == "[Service]\nEnvironment=LOCAL_SETTING=kept\n"
    assert "# >>> cts-scoreboard kiosk (managed) >>>" not in (labwc / "autostart").read_text()
    assert "<!-- >>> cts-scoreboard kiosk (managed) >>> -->" not in (labwc / "rc.xml").read_text()


def test_install_no_wifi_skips_controller_assets(tmp_path):
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    user_systemd = tmp_path / "user-systemd"
    labwc = tmp_path / "labwc"
    desktop = tmp_path / "desktop"
    apps = tmp_path / "apps"
    icons = tmp_path / "icons" / "hicolor" / "scalable" / "apps"
    bin_target = tmp_path / "usr-local-bin"
    controller_dir = tmp_path / "controller"
    system_service_dir = tmp_path / "systemd"

    for path in [home, bin_dir, user_systemd, labwc, desktop, apps, icons, bin_target, controller_dir, system_service_dir]:
        path.mkdir(parents=True, exist_ok=True)
    (home / ".bashrc").write_text("# existing\n")
    (labwc / "autostart").write_text("wf-panel-pi\n")
    (labwc / "rc.xml").write_text("<labwc_config><keyboard></keyboard></labwc_config>\n")

    _make_fake_command(bin_dir, "sudo", "exec \"$@\"")
    _make_fake_command(bin_dir, "systemctl", 'if [ "${3:-}" = Result ]; then echo success; fi\nexit 0')
    _make_fake_command(bin_dir, "apt", "exit 0")
    _make_fake_command(bin_dir, "loginctl", "exit 0")
    _make_fake_command(bin_dir, "gio", "exit 0")
    _make_fake_command(bin_dir, "raspi-config", "if [ \"${2:-}\" = get_autologin ]; then echo 0; fi\nexit 0")
    _make_fake_command(bin_dir, "gtk-update-icon-cache", "exit 0")

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "HOME": str(home),
            "XDG_SESSION_TYPE": "wayland",
            "CTS_USER_SYSTEMD_DIR": str(user_systemd),
            "CTS_LABWC_DIR": str(labwc),
            "CTS_DESKTOP_DIR": str(desktop),
            "CTS_APPLICATIONS_DIR": str(apps),
            "CTS_ICON_DIR": str(icons),
            "CTS_BIN_DIR": str(bin_target),
            "CTS_CONTROLLER_DIR": str(controller_dir),
            "CTS_SYSTEMD_DIR": str(system_service_dir),
            "CTS_SCOREBOARD_GROUP": "scoreboard",
            "CTS_INSTALL_TEST_MODE": "1",
        }
    )

    _run("pi/scripts/install-kiosk.sh", env, "--no-wifi")

    assert not (system_service_dir / "cts-wifi-provisioning.service").exists()
    assert not (controller_dir / "wifi_provisioning.py").exists()
    assert not (bin_target / "cts-wifi-setup").exists()
    assert not (user_systemd / "cts-scoreboard.service.d" / "10-wifi-provisioning.conf").exists()


def test_install_no_apt_requires_wifi_prereqs(tmp_path):
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    user_systemd = tmp_path / "user-systemd"
    labwc = tmp_path / "labwc"
    desktop = tmp_path / "desktop"
    apps = tmp_path / "apps"
    icons = tmp_path / "icons" / "hicolor" / "scalable" / "apps"
    bin_target = tmp_path / "usr-local-bin"
    controller_dir = tmp_path / "controller"
    system_service_dir = tmp_path / "systemd"

    for path in [home, bin_dir, user_systemd, labwc, desktop, apps, icons, bin_target, controller_dir, system_service_dir]:
        path.mkdir(parents=True, exist_ok=True)
    (home / ".bashrc").write_text("# existing\n")
    (labwc / "autostart").write_text("wf-panel-pi\n")
    (labwc / "rc.xml").write_text("<labwc_config><keyboard></keyboard></labwc_config>\n")

    _make_fake_command(bin_dir, "sudo", "exec \"$@\"")
    _make_fake_command(bin_dir, "systemctl", "exit 0")
    _make_fake_command(bin_dir, "loginctl", "exit 0")
    _make_fake_command(bin_dir, "gio", "exit 0")
    _make_fake_command(bin_dir, "raspi-config", "if [ \"${2:-}\" = get_autologin ]; then echo 0; fi\nexit 0")
    _make_fake_command(bin_dir, "gtk-update-icon-cache", "exit 0")

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(home),
            "XDG_SESSION_TYPE": "wayland",
            "CTS_USER_SYSTEMD_DIR": str(user_systemd),
            "CTS_LABWC_DIR": str(labwc),
            "CTS_DESKTOP_DIR": str(desktop),
            "CTS_APPLICATIONS_DIR": str(apps),
            "CTS_ICON_DIR": str(icons),
            "CTS_BIN_DIR": str(bin_target),
            "CTS_CONTROLLER_DIR": str(controller_dir),
            "CTS_SYSTEMD_DIR": str(system_service_dir),
            "CTS_SCOREBOARD_GROUP": "scoreboard",
            "CTS_INSTALL_TEST_MODE": "1",
        }
    )

    result = _run_allow_fail("pi/scripts/install-kiosk.sh", env, "--no-apt")
    assert result.returncode != 0
    assert "missing Wi-Fi provisioning prerequisites" in result.stderr
