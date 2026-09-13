from __future__ import annotations

import subprocess
import threading
import uuid

import pytest

import wifi_provisioning
from tests.test_wifi_provisioning import CommandRunner, FakeBackend, FakeClock, FakePopen, NmStateRunner


class ToggleableCommandRunner(CommandRunner):
    def __init__(self, events):
        super().__init__(events)
        self.fail_delete = False

    def __call__(self, argv, *, timeout, input_text=None):
        self.events.append(("cmd", tuple(argv), input_text))
        if argv[:4] == ["ip", "-4", "route", "show"]:
            return subprocess.CompletedProcess(argv, 0, self.route_output, "")
        if argv[:2] == ["nmcli", "-t"]:
            return subprocess.CompletedProcess(argv, 0, "limited\n", "")
        if argv[:4] == ["nft", "delete", "table", "inet"]:
            if self.fail_delete:
                return subprocess.CompletedProcess(argv, 1, "", "permission denied")
            return subprocess.CompletedProcess(argv, 1, "", "No such file or directory")
        if argv[:3] == ["nft", "-f", "-"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected command: {argv}")


class FailingStageRunner(NmStateRunner):
    def __call__(self, args, timeout=30):
        if args[:2] == ["con", "up"] and args[3].startswith(wifi_provisioning.STAGED_PROFILE_PREFIX):
            self.events.append(("nm", tuple(args)))
            return 10, "", "wrong password"
        return super().__call__(args, timeout=timeout)


class BlockingJoinBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.join_started = threading.Event()
        self.join_release = threading.Event()
        self.restore_setup_called = False

    def connect_network(self, ssid, *, secret=None, hidden=False, profile_id=None, security=None):
        self.calls.append(("connect_network", ssid, secret, hidden, profile_id))
        self.join_started.set()
        if not self.join_release.wait(timeout=5):
            raise AssertionError("join was never released")
        return False, "join failed"

    def restore_setup(self):
        self.restore_setup_called = True


class ScanFailBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.raise_on_scan = False

    def scan_networks(self):
        self.calls.append(("scan_networks",))
        if self.raise_on_scan:
            raise wifi_provisioning.BackendError("NM rescan failed")
        return list(self.scan_results)


@pytest.fixture
def runtime_dir(tmp_path):
    return tmp_path


def _make_backend(runtime_dir, events, nm_runner=None, command_runner=None):
    nm_runner = nm_runner or NmStateRunner(events)
    command_runner = command_runner or ToggleableCommandRunner(events)
    return wifi_provisioning.SystemProvisioningBackend(
        interface="wlan0",
        ap_connection_id=wifi_provisioning.AP_CONNECTION_ID,
        runtime_dir=str(runtime_dir),
        nm_runner=nm_runner,
        command_runner=command_runner,
        popen_factory=lambda argv, **kwargs: FakePopen(argv, events=events),
        sleeper=lambda seconds: None,
    )


def test_system_backend_repeat_activate_setup_reuses_deterministic_ap_uuid_and_only_adds_once(runtime_dir):
    events = []
    nm_runner = NmStateRunner(events)
    nm_runner.ap_exists = False
    backend = _make_backend(runtime_dir, events, nm_runner=nm_runner)

    backend.activate_setup()
    first_adds = [call for call in events if call[0] == "nm" and call[1][:3] == ("con", "add", "type")]
    assert len(first_adds) == 1
    assert nm_runner.ap_uuid == str(uuid.uuid5(uuid.NAMESPACE_URL, "cts-scoreboard/setup/wlan0/cts-scoreboard-ap"))

    events.clear()
    backend.activate_setup()

    second_adds = [call for call in events if call[0] == "nm" and call[1][:3] == ("con", "add", "type")]
    second_modifies = [call for call in events if call[0] == "nm" and call[1][:3] == ("con", "modify", "uuid")]
    assert second_adds == []
    assert second_modifies
    assert all(call[1][3] == nm_runner.ap_uuid for call in second_modifies)


def test_system_backend_shutdown_orders_cleanup_before_autoconnect_restore(runtime_dir):
    events = []
    backend = _make_backend(runtime_dir, events)

    backend.activate_setup()
    events.clear()

    backend.shutdown()

    terminate_index = next(i for i, call in enumerate(events) if call == ("terminate", 4321))
    nft_delete_index = next(i for i, call in enumerate(events) if call[0] == "cmd" and call[1][:4] == ("nft", "delete", "table", "inet"))
    ap_down_index = next(i for i, call in enumerate(events) if call[0] == "nm" and call[1][:3] == ("con", "down", "uuid"))
    ap_delete_index = next(i for i, call in enumerate(events) if call[0] == "nm" and call[1][:3] == ("con", "delete", "uuid"))
    autoconnect_restore_index = next(
        i for i, call in enumerate(events)
        if call[0] == "nm" and call[1][:4] == ("device", "set", "wlan0", "autoconnect") and call[1][-1] == "yes"
    )

    assert terminate_index < nft_delete_index < ap_down_index < ap_delete_index < autoconnect_restore_index


def test_system_backend_shutdown_aborts_on_cleanup_failure_without_restoring_autoconnect(runtime_dir):
    events = []
    command_runner = ToggleableCommandRunner(events)
    backend = _make_backend(runtime_dir, events, command_runner=command_runner)

    backend.activate_setup()
    events.clear()
    command_runner.fail_delete = True

    with pytest.raises(wifi_provisioning.BackendError, match="permission denied"):
        backend.shutdown()

    assert not any(call[0] == "nm" and call[1][:4] == ("device", "set", "wlan0", "autoconnect") and call[1][-1] == "yes" for call in events)
    assert not any(call[0] == "nm" and call[1][:3] == ("con", "delete", "uuid") for call in events)


def test_system_backend_connect_network_stages_wpa3_profile_and_restores_autoconnect_from_normal_mode(runtime_dir):
    events = []
    nm_runner = NmStateRunner(events)
    backend = _make_backend(runtime_dir, events, nm_runner=nm_runner)

    success, message = backend.connect_network("Pool WiFi", secret="secret", hidden=True, security="wpa3")
    assert success is True
    assert message == "Connected to Pool WiFi"

    add_call = next(call for call in events if call[0] == "nm" and call[1][:3] == ("con", "add", "type"))
    add_args = add_call[1]
    assert ("connection.autoconnect", "no") in tuple(zip(add_args, add_args[1:]))
    assert ("wifi-sec.key-mgmt", "sae") in tuple(zip(add_args, add_args[1:]))
    assert ("wifi-sec.psk", "secret") in tuple(zip(add_args, add_args[1:]))
    assert nm_runner.device_autoconnect is False

    backend.finalize_staged_connection()
    backend.restore_device_autoconnect()

    assert nm_runner.device_autoconnect is True
    assert any(call[0] == "nm" and call[1][:4] == ("device", "set", "wlan0", "autoconnect") and call[1][-1] == "yes" for call in events)


def test_system_backend_connect_network_cleans_up_staged_profile_when_activation_fails(runtime_dir):
    events = []
    nm_runner = FailingStageRunner(events)
    backend = _make_backend(runtime_dir, events, nm_runner=nm_runner)

    success, message = backend.connect_network("Pool WiFi", secret="secret", hidden=True)

    assert success is False
    assert message == "wrong password"
    delete_calls = [call for call in events if call[0] == "nm" and call[1][:3] == ("con", "delete", "id")]
    assert delete_calls
    assert delete_calls[-1][1][3].startswith(wifi_provisioning.STAGED_PROFILE_PREFIX)


def test_controller_refresh_failure_preserves_existing_scan_cache():
    backend = ScanFailBackend()
    backend.raise_on_scan = True
    backend.setup_active = False
    backend.snapshot_value["setup_active"] = False
    clock = FakeClock()
    controller = wifi_provisioning.ProvisioningController(
        wifi_provisioning.ProvisioningConfig(
            ap_address="192.168.4.1/24",
            boot_grace=9999.0,
            disconnect_grace=9999.0,
            scan_freshness=999.0,
        ),
        backend=backend,
        clock=clock,
        start_worker=True,
    )
    try:
        controller._scan_cache = [{"ssid": "Cached", "signal": 99, "security": "wpa2", "in_use": False}]
        controller._scan_at = clock.monotonic()

        response = controller.handle("refresh", {"confirmed": True})
        assert response["success"] is True
        assert controller.flush()

        status = controller.status()
        assert status["state"] == wifi_provisioning.STATE_ERROR
        assert "Refresh failed: NM rescan failed" in status["message"]
        assert controller.handle("scan", {})["networks"] == [{"ssid": "Cached", "signal": 99, "security": "wpa2", "in_use": False}]
    finally:
        controller.close()


def test_controller_scan_is_cache_only_and_pending_join_survives_monitor_tick():
    backend = FakeBackend()
    backend.setup_active = False
    backend.snapshot_value["setup_active"] = False
    clock = FakeClock()
    controller = wifi_provisioning.ProvisioningController(
        wifi_provisioning.ProvisioningConfig(ap_address="192.168.4.1/24", operation_timeout=5.0),
        backend=backend,
        clock=clock,
        start_worker=False,
    )
    try:
        controller._scan_cache = [{"ssid": "Cached", "signal": 88, "security": "wpa2", "in_use": False}]
        controller._scan_at = clock.monotonic()

        calls_before = list(backend.calls)
        scan = controller.handle("scan", {})
        assert scan["networks"] == [{"ssid": "Cached", "signal": 88, "security": "wpa2", "in_use": False}]
        assert backend.calls == calls_before

        prepare = controller.handle("prepare_join", {"ssid": "Cafe WiFi", "password": "secret", "security": "wpa2"})
        assert prepare["success"] is True
        calls_before_poll = list(backend.calls)

        controller.poll_once()

        assert controller._queued_operation is None
        assert controller.status()["state"] == wifi_provisioning.STATE_JOIN_PENDING
        assert backend.calls == calls_before_poll
    finally:
        controller.close()


def test_controller_refresh_marks_empty_successful_scan_fresh():
    backend = FakeBackend()
    backend.scan_results = []
    backend.setup_active = False
    backend.snapshot_value["setup_active"] = False
    clock = FakeClock()
    controller = wifi_provisioning.ProvisioningController(
        wifi_provisioning.ProvisioningConfig(
            ap_address="192.168.4.1/24",
            boot_grace=9999.0,
            disconnect_grace=9999.0,
            scan_freshness=999.0,
        ),
        backend=backend,
        clock=clock,
    )
    try:
        response = controller.handle("refresh", {"confirmed": True})
        assert response["success"] is True
        assert controller.flush()

        status = controller.status()
        assert status["scan_stale"] is False
        assert controller.handle("scan", {})["networks"] == []
    finally:
        controller.close()


def test_controller_close_waits_for_active_job_and_does_not_restore_setup_after_stop():
    backend = BlockingJoinBackend()
    backend.setup_active = False
    backend.snapshot_value["setup_active"] = False
    clock = FakeClock()
    controller = wifi_provisioning.ProvisioningController(
        wifi_provisioning.ProvisioningConfig(ap_address="192.168.4.1/24", boot_grace=9999.0, disconnect_grace=9999.0),
        backend=backend,
        clock=clock,
    )
    try:
        prepared = controller.handle("prepare_join", {"ssid": "Cafe WiFi", "password": "secret", "security": "wpa2"})
        assert prepared["success"] is True
        committed = controller.handle("commit_join", {"operation_id": prepared["operation_id"]})
        assert committed["success"] is True
        assert backend.join_started.wait(timeout=2)

        close_thread = threading.Thread(target=controller.close)
        close_thread.start()
        close_thread.join(timeout=0.2)
        assert close_thread.is_alive()

        backend.join_release.set()
        close_thread.join(timeout=5)
        assert not close_thread.is_alive()
        assert backend.restore_setup_called is False
    finally:
        if not controller._closed:
            controller.close()


def test_system_backend_startup_cleanup_preserves_actual_ap_subnet(runtime_dir):
    events = []
    nm_runner = NmStateRunner(events)
    nm_runner.active = "ap"
    nm_runner.ap_address = "192.168.6.1/24"
    backend = _make_backend(runtime_dir, events, nm_runner=nm_runner)

    snapshot = backend.startup_cleanup()

    assert snapshot["ap_network"] == "192.168.6.0/24"
    assert backend._ap_address == "192.168.6.1/24"
    assert snapshot["setup_url"].startswith("http://192.168.6.1:5000/")


def test_monitor_queues_recovery_when_setup_ap_loses_dnsmasq():
    backend = FakeBackend()
    backend.snapshot_value.update({"setup_active": True, "setup_healthy": False, "wifi_mode": "ap"})
    clock = FakeClock()
    controller = wifi_provisioning.ProvisioningController(
        wifi_provisioning.ProvisioningConfig(ap_address="192.168.4.1/24"),
        backend=backend,
        clock=clock,
        start_worker=False,
    )
    try:
        controller.poll_once()

        assert controller._queued_operation is not None
        assert controller._queued_operation.command == "enter_setup"
        assert controller.status()["state"] == wifi_provisioning.STATE_RECOVERING
    finally:
        controller.close()


def test_monitor_does_not_issue_disruptive_scans_when_setup_ap_is_healthy():
    backend = FakeBackend()
    backend.snapshot_value.update({"setup_active": True, "setup_healthy": True, "wifi_mode": "ap"})
    clock = FakeClock()
    controller = wifi_provisioning.ProvisioningController(
        wifi_provisioning.ProvisioningConfig(ap_address="192.168.4.1/24"),
        backend=backend,
        clock=clock,
        start_worker=False,
    )
    try:
        scan_calls_before = sum(1 for call in backend.calls if call[0] == "scan_networks")
        controller.poll_once()
        scan_calls_after = sum(1 for call in backend.calls if call[0] == "scan_networks")

        assert controller._queued_operation is None
        assert scan_calls_after == scan_calls_before
    finally:
        controller.close()
