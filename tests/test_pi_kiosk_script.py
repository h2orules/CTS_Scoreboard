from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def _make_fake_command(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body + "\n")
    path.chmod(0o755)


def test_kiosk_script_wraps_local_home_url(tmp_path):
    bin_dir = tmp_path / "bin"
    profile = tmp_path / "profile"
    log_file = tmp_path / "chromium.log"
    bin_dir.mkdir()
    profile.mkdir()

    _make_fake_command(bin_dir, "curl", "exit 0")
    _make_fake_command(
        bin_dir,
        "chromium",
        f'printf "%s\\n" "$*" >> "{log_file}"\nexit 0',
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "CTS_KIOSK_URL": "/web/home",
            "CTS_KIOSK_PROFILE": str(profile),
            "CTS_KIOSK_BASE_URL": "http://localhost:5000",
            "HOME": str(tmp_path / "home"),
        }
    )

    subprocess.run(
        ["bash", str(REPO / "pi" / "scripts" / "cts-kiosk.sh")],
        cwd=str(REPO),
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )

    args = log_file.read_text()
    assert "--app=http://localhost:5000/web/kiosk?display=%2Fweb%2Fhome" in args


def test_kiosk_script_encodes_query_and_preserves_display(tmp_path):
    bin_dir = tmp_path / "bin"
    profile = tmp_path / "profile"
    log_file = tmp_path / "chromium.log"
    bin_dir.mkdir()
    profile.mkdir()

    _make_fake_command(bin_dir, "curl", "exit 0")
    _make_fake_command(bin_dir, "chromium", f'printf "%s\\n" "$*" >> "{log_file}"\nexit 0')

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "CTS_KIOSK_URL": "/web/home?event=2&heat=3",
            "CTS_KIOSK_PROFILE": str(profile),
            "CTS_KIOSK_BASE_URL": "http://localhost:5000",
            "HOME": str(tmp_path / "home"),
        }
    )

    subprocess.run(
        ["bash", str(REPO / "pi" / "scripts" / "cts-kiosk.sh")],
        cwd=str(REPO),
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )

    args = log_file.read_text()
    assert "--app=http://localhost:5000/web/kiosk?display=%2Fweb%2Fhome%3Fevent%3D2%26heat%3D3" in args


def test_kiosk_script_preserves_external_url(tmp_path):
    bin_dir = tmp_path / "bin"
    profile = tmp_path / "profile"
    log_file = tmp_path / "chromium.log"
    bin_dir.mkdir()
    profile.mkdir()

    _make_fake_command(bin_dir, "chromium", f'printf "%s\\n" "$*" >> "{log_file}"\nexit 0')

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "CTS_KIOSK_URL": "https://example.com/scoreboard",
            "CTS_KIOSK_PROFILE": str(profile),
            "HOME": str(tmp_path / "home"),
        }
    )

    subprocess.run(
        ["bash", str(REPO / "pi" / "scripts" / "cts-kiosk.sh")],
        cwd=str(REPO),
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )

    args = log_file.read_text()
    assert "--app=https://example.com/scoreboard" in args


def test_kiosk_script_does_not_wrap_other_local_hostnames(tmp_path):
    bin_dir = tmp_path / "bin"
    profile = tmp_path / "profile"
    log_file = tmp_path / "chromium.log"
    bin_dir.mkdir()
    profile.mkdir()

    _make_fake_command(bin_dir, "chromium", f'printf "%s\\n" "$*" >> "{log_file}"\nexit 0')

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "CTS_KIOSK_URL": "http://other.local:5000/web/home",
            "CTS_KIOSK_PROFILE": str(profile),
            "HOME": str(tmp_path / "home"),
        }
    )

    subprocess.run(
        ["bash", str(REPO / "pi" / "scripts" / "cts-kiosk.sh")],
        cwd=str(REPO),
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )

    args = log_file.read_text()
    assert "--app=http://other.local:5000/web/home" in args
