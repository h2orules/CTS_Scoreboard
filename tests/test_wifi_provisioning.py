from __future__ import annotations

import grp
import io
import json
import os
import stat
import subprocess
import time
import uuid
from pathlib import Path

import pytest

import wifi_provisioning


class FakeClock:
    def __init__(self):
        self._now = 1_000.0

    def time(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += seconds
        time.sleep(0.001)

    def advance(self, seconds: float) -> None:
        self._now += seconds


class FakeBackend:
    def __init__(self):
        self.calls: list[tuple] = []
        self.available_flag = True
        self.setup_active = True
        self.scan_results = [{"ssid": "Cafe WiFi", "signal": 77, "security": "wpa2", "in_use": False}]
        self.saved_profiles_value = [{"id": "uuid-home", "ssid": "Home WiFi"}]
        self.profiles_value = [
            {"id": "11111111-1111-1111-1111-111111111111", "ssid": "Home WiFi"},
            {"id": "uuid-ap", "ssid": wifi_provisioning.AP_SSID},
        ]
        self.snapshot_value = {
            "setup_active": True,
            "wifi_ssid": None,
            "wifi_signal": None,
            "wifi_profile_id": None,
            "wifi_mode": None,
            "ethernet": False,
            "ipv4": None,
            "hostname": "pool-pi",
            "local_url": "http://pool-pi.local:5000/",
            "setup_url": "http://192.168.4.1:5000/wifi/setup",
            "ap_network": "192.168.4.0/24",
            "connectivity": "unknown",
        }
        self.saved_results = {"uuid-home": (True, "Connected to Home WiFi")}
        self.new_results = {"Cafe WiFi": (True, "Connected to Cafe WiFi")}
        self.stage_finalize_results: list[str] = []
        self.discard_count = 0

    def available(self):
        return self.available_flag

    def startup_cleanup(self):
        self.calls.append(("startup_cleanup",))
        return self.snapshot()

    def shutdown(self):
        self.calls.append(("shutdown",))

    def snapshot(self):
        return dict(self.snapshot_value)

    def scan_networks(self):
        self.calls.append(("scan_networks",))
        return list(self.scan_results)

    def saved_networks(self):
        return [profile["ssid"] for profile in self.saved_profiles_value]

    def saved_profiles(self):
        return list(self.saved_profiles_value)

    def profiles(self):
        self.calls.append(("profiles",))
        return list(self.profiles_value)

    def activate_setup(self):
        self.calls.append(("activate_setup",))
        self.setup_active = True
        self.snapshot_value.update({"setup_active": True, "wifi_ssid": None, "wifi_profile_id": None, "wifi_mode": None, "ipv4": None})

    def deactivate_setup(self, restore_autoconnect=False):
        self.calls.append(("deactivate_setup", restore_autoconnect))
        self.setup_active = False
        self.snapshot_value["setup_active"] = False

    def restore_setup(self):
        self.calls.append(("restore_setup",))
        self.activate_setup()

    def restore_device_autoconnect(self, ignore_missing=False):
        self.calls.append(("restore_device_autoconnect", ignore_missing))

    def connect_saved(self, ssid, profile_id=None):
        self.calls.append(("connect_saved", ssid, profile_id))
        result = self.saved_results.get(profile_id or ssid, (False, f"failed {ssid}"))
        if result[0]:
            self.snapshot_value.update(
                {
                    "setup_active": False,
                    "wifi_ssid": ssid,
                    "wifi_signal": 71,
                    "wifi_profile_id": profile_id,
                    "wifi_mode": "infrastructure",
                    "ipv4": "192.168.10.55",
                }
            )
        return result

    def connect_network(self, ssid, *, secret=None, hidden=False, profile_id=None, security=None):
        self.calls.append(("connect_network", ssid, secret, hidden, profile_id))
        result = self.new_results.get(ssid, (False, f"failed {ssid}"))
        if result[0]:
            self.snapshot_value.update(
                {
                    "setup_active": False,
                    "wifi_ssid": ssid,
                    "wifi_signal": 66,
                    "wifi_profile_id": "uuid-stage",
                    "wifi_mode": "infrastructure",
                    "ipv4": "192.168.10.56",
                }
            )
        return result

    def finalize_staged_connection(self, replaced_profile_id=None):
        self.calls.append(("finalize_staged_connection", replaced_profile_id))
        if self.stage_finalize_results:
            return self.stage_finalize_results.pop(0)
        return None

    def discard_staged_connection(self):
        self.calls.append(("discard_staged_connection",))
        self.discard_count += 1

    def forget(self, ssid, profile_id=None):
        self.calls.append(("forget", ssid, profile_id))
        return True, f"Forgot {profile_id or ssid}"

    def update_password(self, ssid, secret, profile_id=None):
        self.calls.append(("update_password", ssid, secret, profile_id))
        return True, f"Password updated for {profile_id or ssid}"


@pytest.fixture

def fake_backend():
    return FakeBackend()


@pytest.fixture

def controller(fake_backend):
    ctrl = wifi_provisioning.ProvisioningController(
        wifi_provisioning.ProvisioningConfig(ap_address="192.168.4.1/24"),
        backend=fake_backend,
        clock=FakeClock(),
    )
    yield ctrl
    ctrl.close()


def test_controller_commit_join_uses_saved_profile_id_and_restores_autoconnect(controller, fake_backend):
    prepare = controller.handle(
        "prepare_join",
        {"ssid": "Home WiFi", "profile_id": "11111111-1111-1111-1111-111111111111", "security": "wpa2"},
    )
    operation_id = prepare["operation_id"]
    assert prepare["status"]["target_ssid"] == "Home WiFi"
    fake_backend.saved_results = {"11111111-1111-1111-1111-111111111111": (True, "Connected")}

    commit = controller.handle("commit_join", {"operation_id": operation_id})

    assert commit["success"] is True
    assert commit["status"]["target_ssid"] == "Home WiFi"
    assert controller.flush()
    assert fake_backend.calls[2:5] == [
        ("deactivate_setup", False),
        ("connect_saved", "Home WiFi", "11111111-1111-1111-1111-111111111111"),
        ("finalize_staged_connection", None),
    ]
    assert ("finalize_staged_connection", None) in fake_backend.calls
    status = controller.status()
    assert status["state"] == wifi_provisioning.STATE_NORMAL
    assert status["setup_active"] is False
    assert status["wifi_ssid"] == "Home WiFi"
    assert status["target_ssid"] is None
    assert status["message"] == "Connected to Home WiFi"



def test_controller_failed_join_restores_setup_and_surfaces_failure(controller, fake_backend):
    fake_backend.new_results = {"Cafe WiFi": (False, "wrong password")}
    prepare = controller.handle(
        "prepare_join",
        {"ssid": "Cafe WiFi", "password": "secret", "hidden": True, "security": "wpa2"},
    )
    assert prepare["status"]["target_ssid"] == "Cafe WiFi"

    controller.handle("commit_join", {"operation_id": prepare["operation_id"]})

    assert controller.flush()
    assert ("deactivate_setup", False) in fake_backend.calls
    assert ("connect_network", "Cafe WiFi", "secret", True, None) in fake_backend.calls
    assert ("discard_staged_connection",) in fake_backend.calls
    assert ("restore_setup",) in fake_backend.calls
    status = controller.status()
    assert status["state"] == wifi_provisioning.STATE_ERROR
    assert status["setup_active"] is True
    assert status["target_ssid"] is None
    assert "Join failed: wrong password" == status["message"]



def test_controller_refresh_updates_scan_cache_and_restores_setup(controller, fake_backend):
    result = controller.handle("refresh", {"confirmed": True})

    assert result["success"] is True
    assert controller.flush()
    assert fake_backend.calls[2:5] == [
        ("deactivate_setup", False),
        ("scan_networks",),
        ("profiles",),
    ]
    assert ("activate_setup",) in fake_backend.calls
    assert controller.handle("scan", {})["networks"] == fake_backend.scan_results
    assert controller.status()["state"] == wifi_provisioning.STATE_SETUP
    assert controller.status()["setup_active"] is True



def test_controller_pending_join_is_idempotent_and_expires(fake_backend):
    clock = FakeClock()
    ctrl = wifi_provisioning.ProvisioningController(
        wifi_provisioning.ProvisioningConfig(ap_address="192.168.4.1/24", operation_timeout=5.0),
        backend=fake_backend,
        clock=clock,
        start_worker=False,
    )
    try:
        first = ctrl.handle("prepare_join", {"ssid": "Cafe WiFi", "password": "secret"})
        second = ctrl.handle("prepare_join", {"ssid": "Cafe WiFi", "password": "secret"})
        assert first["operation_id"] == second["operation_id"]
        assert ctrl.status()["target_ssid"] == "Cafe WiFi"

        clock.advance(6.0)
        expired = ctrl.handle("commit_join", {"operation_id": first["operation_id"]})
        assert expired["success"] is False
        assert expired["message"] == "No pending join"
        assert "expired" in ctrl.status()["message"]
        assert ctrl.status()["target_ssid"] is None
    finally:
        ctrl.close()


def test_duplicate_completed_commit_returns_existing_operation(controller, fake_backend):
    prepared = controller.handle("prepare_join", {"ssid": "Cafe WiFi", "password": "secret"})
    operation_id = prepared["operation_id"]
    controller.handle("commit_join", {"operation_id": operation_id})
    assert controller.flush()
    calls_before = list(fake_backend.calls)
    repeated = controller.handle("commit_join", {"operation_id": operation_id})
    assert repeated["success"] is True
    assert repeated["operation_id"] == operation_id
    assert repeated["status"]["last_operation"]["success"] is True
    assert fake_backend.calls == calls_before


def test_controller_prepare_join_allows_open_or_auto_security_and_exact_ssid(fake_backend):
    ctrl = wifi_provisioning.ProvisioningController(
        wifi_provisioning.ProvisioningConfig(ap_address="192.168.4.1/24"),
        backend=fake_backend,
        clock=FakeClock(),
        start_worker=False,
    )
    try:
        response = ctrl.handle(
            "prepare_join",
            {"ssid": "  Pool Guest  ", "security": "open", "hidden": True},
        )
        assert response["success"] is True
        assert response["status"]["target_ssid"] == "  Pool Guest  "

        response = ctrl.handle(
            "prepare_join",
            {"ssid": "é" * 17, "security": "auto"},
        )
        assert response["success"] is False
        assert response["message"] == "Invalid SSID"

        response = ctrl.handle(
            "prepare_join",
            {"ssid": "Pool WiFi", "security": "enterprise"},
        )
        assert response["success"] is False
        assert response["message"] == "Unsupported Wi-Fi security type"
    finally:
        ctrl.close()


def test_controller_commit_join_stages_profile_replacement_when_new_password_supplied(fake_backend):
    fake_backend.new_results = {"Home WiFi": (True, "Connected to Home WiFi")}
    ctrl = wifi_provisioning.ProvisioningController(
        wifi_provisioning.ProvisioningConfig(ap_address="192.168.4.1/24"),
        backend=fake_backend,
        clock=FakeClock(),
    )
    try:
        prepare = ctrl.handle(
            "prepare_join",
            {
                "ssid": "Home WiFi",
                "profile_id": "11111111-1111-1111-1111-111111111111",
                "password": "new secret",
                "security": "wpa2",
            },
        )
        assert prepare["success"] is True

        commit = ctrl.handle("commit_join", {"operation_id": prepare["operation_id"]})

        assert commit["success"] is True
        assert ctrl.flush()
        assert ("connect_network", "Home WiFi", "new secret", False, "11111111-1111-1111-1111-111111111111") in fake_backend.calls
        assert ("connect_saved", "Home WiFi", "11111111-1111-1111-1111-111111111111") not in fake_backend.calls
        assert ("finalize_staged_connection", "11111111-1111-1111-1111-111111111111") in fake_backend.calls
    finally:
        ctrl.close()


def test_controller_commit_join_preserves_old_profile_when_staged_replacement_fails(fake_backend):
    fake_backend.new_results = {"Home WiFi": (False, "bad credentials")}
    ctrl = wifi_provisioning.ProvisioningController(
        wifi_provisioning.ProvisioningConfig(ap_address="192.168.4.1/24"),
        backend=fake_backend,
        clock=FakeClock(),
    )
    try:
        prepare = ctrl.handle(
            "prepare_join",
            {
                "ssid": "Home WiFi",
                "profile_id": "11111111-1111-1111-1111-111111111111",
                "password": "new secret",
                "security": "wpa2",
            },
        )
        ctrl.handle("commit_join", {"operation_id": prepare["operation_id"]})

        assert ctrl.flush()
        assert ("connect_network", "Home WiFi", "new secret", False, "11111111-1111-1111-1111-111111111111") in fake_backend.calls
        assert ("finalize_staged_connection", "11111111-1111-1111-1111-111111111111") not in fake_backend.calls
        assert ("discard_staged_connection",) in fake_backend.calls
        assert ("connect_saved", "Home WiFi", "11111111-1111-1111-1111-111111111111") not in fake_backend.calls
    finally:
        ctrl.close()



def test_controller_retry_saved_tears_down_setup_and_restores_ap_on_failure(fake_backend):
    fake_backend.saved_profiles_value = [
        {"id": "uuid-home", "ssid": "Home WiFi"},
        {"id": "uuid-cafe", "ssid": "Cafe WiFi"},
    ]
    fake_backend.saved_results = {
        "uuid-home": (False, "wrong password"),
        "uuid-cafe": (False, "dhcp timeout"),
    }
    ctrl = wifi_provisioning.ProvisioningController(
        wifi_provisioning.ProvisioningConfig(ap_address="192.168.4.1/24"),
        backend=fake_backend,
        clock=FakeClock(),
    )
    try:
        result = ctrl.handle("retry_saved", {})
        assert result["success"] is True
        assert ctrl.flush()
        assert fake_backend.calls[2:6] == [
            ("deactivate_setup", False),
            ("connect_saved", "Home WiFi", "uuid-home"),
            ("connect_saved", "Cafe WiFi", "uuid-cafe"),
            ("restore_setup",),
        ]
        status = ctrl.status()
        assert status["state"] == wifi_provisioning.STATE_ERROR
        assert status["setup_active"] is True
        assert "Saved-network retry failed" in status["message"]
    finally:
        ctrl.close()



def test_monitor_queues_setup_after_boot_grace_without_connectivity(fake_backend):
    fake_backend.setup_active = False
    fake_backend.snapshot_value["setup_active"] = False
    clock = FakeClock()
    ctrl = wifi_provisioning.ProvisioningController(
        wifi_provisioning.ProvisioningConfig(ap_address="192.168.4.1/24", boot_grace=3.0, disconnect_grace=2.0),
        backend=fake_backend,
        clock=clock,
        start_worker=False,
    )
    try:
        clock.advance(4.0)
        ctrl.poll_once()
        assert ctrl._queued_operation is not None
        assert ctrl._queued_operation.command == "enter_setup"
        assert ctrl.status()["state"] == wifi_provisioning.STATE_SETUP
    finally:
        ctrl.close()



def test_monitor_does_not_queue_setup_when_ethernet_is_usable(fake_backend):
    fake_backend.setup_active = False
    fake_backend.snapshot_value.update({"setup_active": False, "ethernet": True, "ipv4": None})
    clock = FakeClock()
    ctrl = wifi_provisioning.ProvisioningController(
        wifi_provisioning.ProvisioningConfig(ap_address="192.168.4.1/24", boot_grace=1.0, disconnect_grace=1.0),
        backend=fake_backend,
        clock=clock,
        start_worker=False,
    )
    try:
        clock.advance(3.0)
        ctrl.poll_once()
        assert ctrl._queued_operation is None
        assert ctrl.status()["state"] == wifi_provisioning.STATE_NORMAL
    finally:
        ctrl.close()


class NmStateRunner:
    def __init__(self, events):
        self.events = events
        self.device_autoconnect = True
        self.ap_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, "cts-scoreboard/setup/wlan0/cts-scoreboard-ap"))
        self.ap_exists = True
        self.wifi_uuid = "11111111-1111-1111-1111-111111111111"
        self.ap_address = "192.168.4.1/24"
        self.active = None
        self.wifi_ssid = "Home WiFi"
        self.wifi_ipv4 = ["192.168.10.55"]
        self.eth_ipv4 = ["169.254.10.2"]

    def __call__(self, args, timeout=30):
        self.events.append(("nm", tuple(args)))
        if args[:2] == ["--escape", "no"]:
            args = args[2:]
        if args == ["-g", "UUID", "con", "show"]:
            return 0, (self.ap_uuid + "\n" if self.ap_exists else "") + self.wifi_uuid + "\n", ""
        if args[:2] == ["-g", "GENERAL.TYPE,GENERAL.NM-MANAGED,GENERAL.STATE,WIFI-PROPERTIES.AP"]:
            return 0, "wifi\nyes\n30 (disconnected)\nyes\n", ""
        if args[:5] == ["-t", "-f", "GENERAL.AUTOCONNECT", "dev", "show"]:
            return 0, f"GENERAL.AUTOCONNECT:{'yes' if self.device_autoconnect else 'no'}\n", ""
        if args[:2] == ["device", "set"]:
            self.device_autoconnect = args[-1] == "yes"
            return 0, "", ""
        if args[:3] == ["con", "add", "type"]:
            if "connection.uuid" in args and args[args.index("connection.uuid") + 1] == self.ap_uuid:
                self.ap_exists = True
                self.ap_address = args[args.index("ipv4.addresses") + 1]
            return 0, "added\n", ""
        if args[:2] == ["con", "modify"]:
            if args[3] == self.ap_uuid:
                self.ap_address = args[args.index("ipv4.addresses") + 1]
            return 0, "", ""
        if args[:2] == ["con", "up"]:
            if args[3] == self.ap_uuid:
                self.active = "ap"
            return 0, "", ""
        if args[:2] == ["con", "down"]:
            if args[3] == self.ap_uuid:
                self.active = None
            return 0, "", ""
        if args[:2] == ["con", "delete"]:
            if args[3] == self.ap_uuid:
                self.ap_exists = False
            return 0, "", ""
        if args[:7] == [
            "-t",
            "-f",
            "UUID,NAME,TYPE,DEVICE",
            "con",
            "show",
            "--active",
        ]:
            if self.active == "ap":
                return 0, f"{self.ap_uuid}:{wifi_provisioning.AP_CONNECTION_ID}:802-11-wireless:wlan0\n", ""
            if self.active == "wifi":
                return 0, f"{self.wifi_uuid}:Home Profile:802-11-wireless:wlan0\n", ""
            return 0, "", ""
        if args[:7] == [
            "-t",
            "-f",
            "DEVICE,TYPE,STATE,CONNECTION",
            "dev",
            "status",
        ]:
            if self.active == "ap":
                return 0, f"wlan0:wifi:connected:{wifi_provisioning.AP_CONNECTION_ID}\neth0:ethernet:connected:Wired\n", ""
            if self.active == "wifi":
                return 0, "wlan0:wifi:connected:Home Profile\neth0:ethernet:connected:Wired\n", ""
            return 0, "wlan0:wifi:disconnected:\neth0:ethernet:connected:Wired\n", ""
        if args[:6] == ["-t", "-f", "IP4.ADDRESS", "dev", "show", "wlan0"]:
            if self.active == "ap":
                return 0, f"IP4.ADDRESS[1]:{self.ap_address}\n", ""
            if self.active == "wifi":
                return 0, "".join(f"IP4.ADDRESS[{i + 1}]:{value}/24\n" for i, value in enumerate(self.wifi_ipv4)), ""
            return 0, "", ""
        if args[:6] == ["-t", "-f", "IP4.ADDRESS", "dev", "show", "eth0"]:
            return 0, "".join(f"IP4.ADDRESS[{i + 1}]:{value}/16\n" for i, value in enumerate(self.eth_ipv4)), ""
        if args[:6] == ["-t", "-f", "IN-USE,SIGNAL", "dev", "wifi", "list"]:
            if self.active == "wifi":
                return 0, "*:74\n", ""
            return 0, "", ""
        if args[:4] == [
            "-g",
            "connection.id,connection.uuid,connection.type,connection.autoconnect,connection.interface-name,802-11-wireless.ssid,802-11-wireless.mode",
            "con",
            "show",
        ]:
            target = args[5]
            if target in {wifi_provisioning.AP_CONNECTION_ID, self.ap_uuid}:
                return 0, f"{wifi_provisioning.AP_CONNECTION_ID}\n{self.ap_uuid}\n802-11-wireless\nno\nwlan0\n{wifi_provisioning.AP_SSID}\nap\n", ""
            return 0, f"Home Profile\n{self.wifi_uuid}\n802-11-wireless\nyes\nwlan0\n{self.wifi_ssid}\ninfrastructure\n", ""
        return 0, "", ""


class CommandRunner:
    def __init__(self, events):
        self.events = events
        self.route_output = "192.168.4.0/24 dev eth0\ndefault via 192.168.4.1 dev eth0\n"

    def __call__(self, argv, *, timeout, input_text=None):
        self.events.append(("cmd", tuple(argv), input_text))
        if argv[:4] == ["ip", "-4", "route", "show"]:
            return subprocess.CompletedProcess(argv, 0, self.route_output, "")
        if argv[:2] == ["nmcli", "-t"]:
            return subprocess.CompletedProcess(argv, 0, "limited\n", "")
        if argv[:4] == ["nft", "delete", "table", "inet"]:
            return subprocess.CompletedProcess(argv, 1, "", "No such file or directory")
        if argv[:3] == ["nft", "-f", "-"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected command: {argv}")


class FakePopen:
    def __init__(self, argv, *, events):
        self.argv = argv
        self.events = events
        self.events.append(("popen", tuple(argv)))
        self.returncode = None
        self.pid = 4321
        self.stderr = io.StringIO("")

    def poll(self):
        return self.returncode

    def terminate(self):
        self.events.append(("terminate", self.pid))
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.events.append(("kill", self.pid))
        self.returncode = -9


@pytest.fixture

def runtime_dir(tmp_path):
    return tmp_path



def test_system_backend_activate_setup_uses_nonconflicting_subnet_and_scoped_services(runtime_dir):
    events = []
    nm_runner = NmStateRunner(events)
    command_runner = CommandRunner(events)
    backend = wifi_provisioning.SystemProvisioningBackend(
        interface="wlan0",
        ap_connection_id=wifi_provisioning.AP_CONNECTION_ID,
        ap_ssid=wifi_provisioning.AP_SSID,
        ap_address="192.168.4.1/24",
        runtime_dir=str(runtime_dir),
        nm_runner=nm_runner,
        command_runner=command_runner,
        popen_factory=lambda argv, **kwargs: FakePopen(argv, events=events),
        sleeper=lambda seconds: None,
    )

    backend.activate_setup()

    assert nm_runner.device_autoconnect is False
    modify_calls = [call for call in events if call[0] == "nm" and call[1][:3] == ("con", "modify", "uuid")]
    assert any("192.168.5.1/24" in call[1] for call in modify_calls)
    dnsmasq_call = next(call for call in events if call[0] == "popen")
    assert "--listen-address" in dnsmasq_call[1]
    assert "192.168.5.1" in dnsmasq_call[1]
    nft_call = next(call for call in events if call[0] == "cmd" and call[1][:3] == ("nft", "-f", "-"))
    assert 'iifname "wlan0" ip saddr 192.168.5.0/24 tcp dport 80 redirect to :5000' in (nft_call[2] or "")
    assert "--conf-file=/dev/null" in dnsmasq_call[1]
    assert 'forward iifname "wlan0" drop' in nft_call[2]



def test_system_backend_deactivate_setup_stops_dnsmasq_and_nft_before_ap_down(runtime_dir):
    events = []
    nm_runner = NmStateRunner(events)
    command_runner = CommandRunner(events)
    backend = wifi_provisioning.SystemProvisioningBackend(
        interface="wlan0",
        ap_connection_id=wifi_provisioning.AP_CONNECTION_ID,
        runtime_dir=str(runtime_dir),
        nm_runner=nm_runner,
        command_runner=command_runner,
        popen_factory=lambda argv, **kwargs: FakePopen(argv, events=events),
        sleeper=lambda seconds: None,
    )
    backend.activate_setup()
    events.clear()

    backend.deactivate_setup()

    terminate_index = events.index(("terminate", 4321))
    delete_index = next(i for i, call in enumerate(events) if call[0] == "cmd" and call[1][:4] == ("nft", "delete", "table", "inet"))
    down_index = next(i for i, call in enumerate(events) if call[0] == "nm" and call[1][:3] == ("con", "down", "uuid"))
    assert terminate_index < down_index
    assert delete_index < down_index



def test_system_backend_snapshot_uses_real_ssid_and_rejects_link_local(runtime_dir):
    events = []
    nm_runner = NmStateRunner(events)
    nm_runner.active = "wifi"
    nm_runner.eth_ipv4 = ["169.254.44.1"]
    nm_runner.wifi_ipv4 = ["169.254.22.5"]
    command_runner = CommandRunner(events)
    backend = wifi_provisioning.SystemProvisioningBackend(
        interface="wlan0",
        ap_connection_id=wifi_provisioning.AP_CONNECTION_ID,
        runtime_dir=str(runtime_dir),
        nm_runner=nm_runner,
        command_runner=command_runner,
        sleeper=lambda seconds: None,
    )

    snapshot = backend.snapshot()

    assert snapshot["wifi_ssid"] == "Home WiFi"
    assert snapshot["ipv4"] is None
    assert snapshot["ethernet"] is False
    assert snapshot["setup_active"] is False



def test_system_backend_connect_network_cleans_up_failed_stage_profile(runtime_dir):
    events = []
    nm_runner = NmStateRunner(events)

    def failing_nm(args, timeout=30):
        if args[:3] == ["con", "up", "id"] and args[3].startswith(wifi_provisioning.STAGED_PROFILE_PREFIX):
            events.append(("nm", tuple(args)))
            return 10, "", "wrong password"
        return nm_runner(args, timeout=timeout)

    backend = wifi_provisioning.SystemProvisioningBackend(
        interface="wlan0",
        ap_connection_id=wifi_provisioning.AP_CONNECTION_ID,
        runtime_dir=str(runtime_dir),
        nm_runner=failing_nm,
        command_runner=CommandRunner(events),
        sleeper=lambda seconds: None,
    )

    success, message = backend.connect_network("Pool WiFi", secret="secret", hidden=True)

    assert success is False
    assert message == "wrong password"
    delete_calls = [call for call in events if call[0] == "nm" and call[1][:3] == ("con", "delete", "id")]
    assert delete_calls
    assert delete_calls[-1][1][3].startswith(wifi_provisioning.STAGED_PROFILE_PREFIX)


def test_system_backend_connect_network_retires_old_profile_only_after_finalize(runtime_dir):
    events = []
    nm_runner = NmStateRunner(events)
    backend = wifi_provisioning.SystemProvisioningBackend(
        interface="wlan0",
        ap_connection_id=wifi_provisioning.AP_CONNECTION_ID,
        runtime_dir=str(runtime_dir),
        nm_runner=nm_runner,
        command_runner=CommandRunner(events),
        sleeper=lambda seconds: None,
    )

    success, message = backend.connect_network(
        "Pool WiFi",
        secret="new secret",
        hidden=False,
        profile_id="11111111-1111-1111-1111-111111111111",
    )

    assert success is True
    assert message == "Connected to Pool WiFi"
    old_profile_deletes = [
        call for call in events
        if call[0] == "nm" and call[1][:4] == ("con", "delete", "uuid", "11111111-1111-1111-1111-111111111111")
    ]
    assert old_profile_deletes == []

    backend.finalize_staged_connection("11111111-1111-1111-1111-111111111111")

    assert ("nm", ("con", "modify", "uuid", "11111111-1111-1111-1111-111111111111", "connection.autoconnect", "no")) in events
    assert ("nm", ("con", "delete", "uuid", "11111111-1111-1111-1111-111111111111")) in events


def test_system_backend_activate_setup_rolls_back_after_activation_failure(runtime_dir):
    events = []
    nm_runner = NmStateRunner(events)

    def failing_nm(args, timeout=30):
        if args[:3] == ["con", "up", "uuid"] and args[3] == nm_runner.ap_uuid:
            events.append(("nm", tuple(args)))
            return 10, "", "activation failed"
        return nm_runner(args, timeout=timeout)

    backend = wifi_provisioning.SystemProvisioningBackend(
        interface="wlan0",
        ap_connection_id=wifi_provisioning.AP_CONNECTION_ID,
        runtime_dir=str(runtime_dir),
        runtime_group=grp.getgrgid(os.getgid()).gr_name,
        nm_runner=failing_nm,
        command_runner=CommandRunner(events),
        sleeper=lambda seconds: None,
    )

    with pytest.raises(wifi_provisioning.BackendError, match="activation failed"):
        backend.activate_setup()

    assert nm_runner.device_autoconnect is True
    assert nm_runner.active is None


def test_system_backend_activate_setup_rolls_back_after_dnsmasq_failure(runtime_dir):
    events = []
    nm_runner = NmStateRunner(events)

    class FailingPopen(FakePopen):
        def __init__(self, argv, *, events):
            super().__init__(argv, events=events)
            self.returncode = 1
            self.stderr = io.StringIO("dnsmasq bind failed")

    backend = wifi_provisioning.SystemProvisioningBackend(
        interface="wlan0",
        ap_connection_id=wifi_provisioning.AP_CONNECTION_ID,
        runtime_dir=str(runtime_dir),
        nm_runner=nm_runner,
        command_runner=CommandRunner(events),
        popen_factory=lambda argv, **kwargs: FailingPopen(argv, events=events),
        sleeper=lambda seconds: None,
    )

    with pytest.raises(wifi_provisioning.BackendError, match="dnsmasq bind failed"):
        backend.activate_setup()

    assert nm_runner.device_autoconnect is True
    assert ("nm", ("con", "down", "uuid", nm_runner.ap_uuid)) in events


def test_system_backend_activate_setup_rolls_back_after_nft_failure(runtime_dir):
    events = []
    nm_runner = NmStateRunner(events)

    class NftFailRunner(CommandRunner):
        def __call__(self, argv, *, timeout, input_text=None):
            self.events.append(("cmd", tuple(argv), input_text))
            if argv[:4] == ["ip", "-4", "route", "show"]:
                return subprocess.CompletedProcess(argv, 0, self.route_output, "")
            if argv[:2] == ["nmcli", "-t"]:
                return subprocess.CompletedProcess(argv, 0, "limited\n", "")
            if argv[:4] == ["nft", "delete", "table", "inet"]:
                return subprocess.CompletedProcess(argv, 1, "", "No such file or directory")
            if argv[:3] == ["nft", "-f", "-"]:
                return subprocess.CompletedProcess(argv, 1, "", "nft insert failed")
            raise AssertionError(f"unexpected command: {argv}")

    backend = wifi_provisioning.SystemProvisioningBackend(
        interface="wlan0",
        ap_connection_id=wifi_provisioning.AP_CONNECTION_ID,
        runtime_dir=str(runtime_dir),
        nm_runner=nm_runner,
        command_runner=NftFailRunner(events),
        popen_factory=lambda argv, **kwargs: FakePopen(argv, events=events),
        sleeper=lambda seconds: None,
    )

    with pytest.raises(wifi_provisioning.BackendError, match="nft insert failed"):
        backend.activate_setup()

    assert nm_runner.device_autoconnect is True
    assert ("terminate", 4321) in events
    assert ("nm", ("con", "down", "uuid", nm_runner.ap_uuid)) in events


def test_system_backend_activate_setup_keeps_autoconnect_disabled_when_cleanup_fails(runtime_dir):
    events = []
    nm_runner = NmStateRunner(events)

    class CleanupFailRunner(CommandRunner):
        def __call__(self, argv, *, timeout, input_text=None):
            self.events.append(("cmd", tuple(argv), input_text))
            if argv[:4] == ["ip", "-4", "route", "show"]:
                return subprocess.CompletedProcess(argv, 0, self.route_output, "")
            if argv[:2] == ["nmcli", "-t"]:
                return subprocess.CompletedProcess(argv, 0, "limited\n", "")
            if argv[:4] == ["nft", "delete", "table", "inet"]:
                return subprocess.CompletedProcess(argv, 1, "", "permission denied")
            if argv[:3] == ["nft", "-f", "-"]:
                return subprocess.CompletedProcess(argv, 1, "", "nft insert failed")
            raise AssertionError(f"unexpected command: {argv}")

    backend = wifi_provisioning.SystemProvisioningBackend(
        interface="wlan0",
        ap_connection_id=wifi_provisioning.AP_CONNECTION_ID,
        runtime_dir=str(runtime_dir),
        nm_runner=nm_runner,
        command_runner=CleanupFailRunner(events),
        popen_factory=lambda argv, **kwargs: FakePopen(argv, events=events),
        sleeper=lambda seconds: None,
    )

    with pytest.raises(wifi_provisioning.BackendError, match=r"permission denied; cleanup: nft cleanup failed: permission denied"):
        backend.activate_setup()

    assert nm_runner.device_autoconnect is False
    assert ("terminate", 4321) in events


def test_system_backend_startup_cleanup_rolls_back_partial_setup_failure(runtime_dir):
    events = []
    nm_runner = NmStateRunner(events)
    nm_runner.active = "ap"

    class FailingPopen(FakePopen):
        def __init__(self, argv, *, events):
            super().__init__(argv, events=events)
            self.returncode = 1
            self.stderr = io.StringIO("dnsmasq startup failed")

    backend = wifi_provisioning.SystemProvisioningBackend(
        interface="wlan0",
        ap_connection_id=wifi_provisioning.AP_CONNECTION_ID,
        runtime_dir=str(runtime_dir),
        nm_runner=nm_runner,
        command_runner=CommandRunner(events),
        popen_factory=lambda argv, **kwargs: FailingPopen(argv, events=events),
        sleeper=lambda seconds: None,
    )

    with pytest.raises(wifi_provisioning.BackendError, match="dnsmasq startup failed"):
        backend.startup_cleanup()

    assert nm_runner.device_autoconnect is True
    assert ("nm", ("con", "down", "uuid", nm_runner.ap_uuid)) in events


def test_system_backend_ensure_runtime_dir_sets_group_permissions(runtime_dir):
    root = runtime_dir / "fresh-root"
    target = root / "control"
    group_name = grp.getgrgid(os.getgid()).gr_name
    backend = wifi_provisioning.SystemProvisioningBackend(
        interface="wlan0",
        ap_connection_id=wifi_provisioning.AP_CONNECTION_ID,
        runtime_dir=str(target),
        runtime_group=group_name,
        nm_runner=NmStateRunner([]),
        command_runner=CommandRunner([]),
        sleeper=lambda seconds: None,
    )

    backend._ensure_runtime_dir()

    info = os.stat(target)
    assert stat.S_IMODE(info.st_mode) == 0o750
    assert info.st_gid == os.getgid()


def test_restart_removes_only_tagged_unfinished_profiles(runtime_dir, monkeypatch):
    profiles = [
        {"id": "pending", "name": "cts-stage-pending"},
        {"id": "ready", "name": "cts-stage-ready"},
        {"id": "unrelated", "name": "cts-stage-user-created"},
    ]
    monkeypatch.setattr(wifi_provisioning.wifi_manager, "list_connection_profiles",
                        lambda **kwargs: profiles)
    calls = []

    def runner(args, timeout=30):
        calls.append(args)
        if args[:2] == ["-g", "user.data"]:
            role = {"pending": "staging", "ready": "ready", "unrelated": ""}
            return 0, f"org.cts-scoreboard.role = {role[args[-1]]}\n", ""
        return 0, "", ""

    backend = wifi_provisioning.SystemProvisioningBackend(
        runtime_dir=str(runtime_dir), nm_runner=runner,
    )
    backend._cleanup_interrupted_profiles()
    assert [args for args in calls if args[:2] == ["con", "delete"]] == [
        ["con", "delete", "id", "pending"],
    ]


def test_ethernet_on_default_setup_subnet_still_suppresses_fallback(runtime_dir):
    nm_runner = NmStateRunner([])
    nm_runner.eth_ipv4 = ["192.168.4.55"]
    backend = wifi_provisioning.SystemProvisioningBackend(
        runtime_dir=str(runtime_dir), nm_runner=nm_runner,
        command_runner=CommandRunner([]),
    )
    snapshot = backend.snapshot()
    assert snapshot["ethernet"] is True
    assert snapshot["ipv4"] == "192.168.4.55"
    assert snapshot["wifi_ipv4"] is None


def test_system_rescan_failure_is_not_an_empty_success(runtime_dir):
    def runner(args, timeout=30):
        assert args[:3] == ["dev", "wifi", "rescan"]
        return 10, "", "scan unavailable"

    backend = wifi_provisioning.SystemProvisioningBackend(
        runtime_dir=str(runtime_dir), nm_runner=runner,
    )
    with pytest.raises(wifi_provisioning.BackendError, match="scan unavailable"):
        backend.scan_networks()


def test_failed_staged_profile_deletion_retains_cleanup_reference(runtime_dir):
    def runner(args, timeout=30):
        assert args[:3] == ["con", "delete", "id"]
        return 10, "", "permission denied"

    backend = wifi_provisioning.SystemProvisioningBackend(
        runtime_dir=str(runtime_dir), nm_runner=runner,
    )
    backend._staged_profile_name = "cts-stage-failed"
    with pytest.raises(wifi_provisioning.BackendError, match="Failed to remove staged credentials"):
        backend.discard_staged_connection()
    assert backend._staged_profile_name == "cts-stage-failed"
