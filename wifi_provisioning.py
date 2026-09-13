"""Serialized Wi-Fi provisioning controller and Unix socket server."""

from __future__ import annotations

import argparse
import fcntl
import grp
import ipaddress
import json
import os
import signal
import socket
import stat
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

import wifi_manager

AP_SSID = "CTS-Scoreboard"
AP_CONNECTION_ID = "cts-scoreboard-ap"
DEFAULT_INTERFACE = "wlan0"
DEFAULT_SOCKET = "/run/cts-scoreboard-wifi/control.sock"
DEFAULT_AP_ADDRESS = "192.168.4.1/24"
DEFAULT_AP_PORT = 5000
DEFAULT_BOOT_GRACE = 20.0
DEFAULT_DISCONNECT_GRACE = 15.0
DEFAULT_SCAN_FRESHNESS = 120.0
DEFAULT_OPERATION_TIMEOUT = 120.0
DEFAULT_REFRESH_BACKOFF = 2.0
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MONITOR_INTERVAL = 1.0
STATE_FILE_NAME = "runtime-state.json"
DNSMASQ_PID_FILE_NAME = "dnsmasq.pid"
DNSMASQ_LEASE_FILE_NAME = "dnsmasq.leases"
NFT_TABLE_NAME = "cts_scoreboard_wifi"
STAGED_PROFILE_PREFIX = "cts-stage-"

STATE_STARTING = "starting"
STATE_NORMAL = "normal"
STATE_SETUP = "setup"
STATE_JOIN_PENDING = "join_pending"
STATE_JOINING = "joining"
STATE_REFRESHING = "refreshing"
STATE_RECOVERING = "recovering"
STATE_ERROR = "error"


class BackendError(RuntimeError):
    """Raised when the system backend cannot complete a provisioning action."""


@dataclass(frozen=True)
class ProvisioningConfig:
    interface: str = DEFAULT_INTERFACE
    socket_path: str = DEFAULT_SOCKET
    socket_group: str | None = None
    ap_address: str = DEFAULT_AP_ADDRESS
    ap_ssid: str = AP_SSID
    ap_connection_id: str = AP_CONNECTION_ID
    ap_port: int = DEFAULT_AP_PORT
    boot_grace: float = DEFAULT_BOOT_GRACE
    disconnect_grace: float = DEFAULT_DISCONNECT_GRACE
    scan_freshness: float = DEFAULT_SCAN_FRESHNESS
    operation_timeout: float = DEFAULT_OPERATION_TIMEOUT
    refresh_backoff: float = DEFAULT_REFRESH_BACKOFF


@dataclass
class Operation:
    id: str
    state: str
    command: str
    params: dict[str, Any]
    func: Callable[[], dict[str, Any]]
    queued_at: float
    result: dict[str, Any] | None = None
    done: bool = False


@dataclass
class PendingJoin:
    operation_id: str
    ssid: str
    password: str | None
    hidden: bool
    security: str | None
    profile_id: str | None
    prepared_at: float
    committed: bool = False


class _DefaultClock:
    def time(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class SystemProvisioningBackend:
    def __init__(
        self,
        interface: str = DEFAULT_INTERFACE,
        ap_connection_id: str = AP_CONNECTION_ID,
        *,
        ap_ssid: str = AP_SSID,
        ap_address: str = DEFAULT_AP_ADDRESS,
        ap_port: int = DEFAULT_AP_PORT,
        runtime_dir: str | None = None,
        runtime_group: str | None = None,
        nm_runner: Callable[..., tuple[int, str, str]] | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        popen_factory: Callable[..., subprocess.Popen[str]] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ):
        self.interface = interface
        self.ap_connection_id = ap_connection_id
        self._ap_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"cts-scoreboard/setup/{interface}/{ap_connection_id}"))
        self.ap_ssid = ap_ssid
        self.ap_port = ap_port
        self._configured_ap_address = ap_address
        self._ap_address = ap_address
        self.runtime_dir = runtime_dir or os.path.dirname(DEFAULT_SOCKET)
        self.runtime_group = runtime_group
        self._nm_runner = nm_runner or wifi_manager._run
        self._run_command = command_runner or self._default_command_runner
        self._popen_factory = popen_factory or subprocess.Popen
        self._sleeper = sleeper or time.sleep
        self._dnsmasq_process: subprocess.Popen[str] | None = None
        self._staged_profile_name: str | None = None

    def available(self) -> bool:
        return (
            wifi_manager.is_available()
            and self._has_binary("dnsmasq")
            and self._has_binary("nft")
            and self._has_binary("ip")
        )

    def startup_cleanup(self) -> dict[str, Any]:
        self._ensure_runtime_dir()
        self._stop_dnsmasq(ignore_missing=True)
        self._remove_nft(ignore_missing=True)
        self._cleanup_interrupted_profiles()
        self._ap_address = self._select_ap_address()
        snapshot = self.snapshot()
        if snapshot.get("setup_active"):
            self.activate_setup()
            snapshot = self.snapshot()
        elif self._load_saved_device_autoconnect() is not None:
            self.restore_device_autoconnect()
        return snapshot

    def shutdown(self) -> None:
        self.deactivate_setup()
        self.discard_staged_connection()
        if self._ap_profile():
            ok, message = wifi_manager.forget(self._ap_uuid, runner=self._nm_runner)
            if not ok:
                raise BackendError(message)
        self.restore_device_autoconnect(ignore_missing=True)

    def snapshot(self) -> dict[str, Any]:
        hostname = socket.gethostname()
        local_url = f"http://{hostname}.local:{self.ap_port}/"
        ap_interface = ipaddress.ip_interface(self._ap_address)
        if not isinstance(ap_interface, ipaddress.IPv4Interface):
            raise BackendError("AP address must be IPv4")
        excluded = [ipaddress.IPv4Network(f"{ap_interface.ip}/32")]
        ap_profile = self._ap_profile()
        active_wifi = self._active_wifi_connection()
        setup_active = (
            active_wifi is not None
            and active_wifi.get("id") == ap_profile.get("id")
            and active_wifi.get("mode") == "ap"
        )
        wifi_ssid: str | None = None
        wifi_signal: int | None = None
        wifi_profile_id: str | None = None
        wifi_mode: str | None = None
        ipv4: str | None = None
        if active_wifi and not setup_active and active_wifi.get("mode") != "ap":
            wifi_ssid = active_wifi.get("ssid") or active_wifi.get("name")
            wifi_signal = wifi_manager._active_wifi_signal(runner=self._read_nmcli)
            wifi_profile_id = active_wifi.get("id")
            wifi_mode = active_wifi.get("mode")
            ipv4 = self._usable_ipv4(self.interface, excluded_networks=excluded)
        ethernet_ipv4 = self._ethernet_ipv4(excluded_networks=[])
        return {
            "setup_active": setup_active,
            "setup_healthy": setup_active and self._dnsmasq_process is not None and self._dnsmasq_process.poll() is None,
            "wifi_ssid": wifi_ssid,
            "wifi_signal": wifi_signal,
            "wifi_profile_id": wifi_profile_id,
            "wifi_mode": wifi_mode,
            "ethernet": ethernet_ipv4 is not None,
            "wifi_ipv4": ipv4,
            "ipv4": ipv4 or ethernet_ipv4,
            "hostname": hostname,
            "local_url": local_url,
            "setup_url": f"http://{ap_interface.ip}:{self.ap_port}/wifi/setup",
            "ap_network": str(ap_interface.network),
            "connectivity": self._connectivity(),
        }

    def scan_networks(self) -> list[dict[str, Any]]:
        return wifi_manager.scan_networks(runner=self._read_nmcli, interface=self.interface)

    def saved_networks(self) -> list[str]:
        return [profile["ssid"] for profile in self.saved_profiles()]

    def saved_profiles(self) -> list[dict[str, Any]]:
        seen: set[str] = set()
        profiles: list[dict[str, Any]] = []
        for profile in self.profiles():
            profile_id = profile.get("id")
            ssid = profile.get("ssid")
            if not isinstance(profile_id, str) or not isinstance(ssid, str):
                continue
            key = profile_id.lower()
            if key in seen:
                continue
            seen.add(key)
            profiles.append(profile)
        profiles.sort(key=self._saved_profile_priority, reverse=True)
        return profiles

    def _saved_profile_priority(self, profile: dict[str, Any]) -> tuple[int, int]:
        _, output, _ = self._read_nmcli([
            "-g", "connection.autoconnect-priority,connection.timestamp",
            "con", "show", "uuid", profile["id"],
        ])
        values = output.splitlines()
        try:
            if len(values) != 2:
                raise ValueError("Missing profile priority")
            return int(values[0]), int(values[1])
        except ValueError as error:
            raise BackendError("Cannot read saved Wi-Fi profile priority") from error

    def profiles(self) -> list[dict[str, Any]]:
        profiles: list[dict[str, Any]] = []
        for profile in wifi_manager.list_connection_profiles(runner=self._read_nmcli):
            if profile.get("id") == self._ap_uuid or profile.get("mode") == "ap":
                continue
            if profile.get("device") not in (None, "", self.interface):
                continue
            if profile.get("name") == self._staged_profile_name:
                continue
            profiles.append({"id": profile.get("id"), "ssid": profile.get("ssid")})
        return profiles

    def connect_saved(self, ssid: str, profile_id: str | None = None) -> tuple[bool, str]:
        target = profile_id or ssid
        return wifi_manager.activate_connection(target, interface=self.interface, runner=self._nm_runner)

    def connect_network(
        self,
        ssid: str,
        *,
        secret: str | None = None,
        hidden: bool = False,
        profile_id: str | None = None,
        security: str | None = None,
    ) -> tuple[bool, str]:
        self._ensure_setup_autoconnect_inhibited()
        if profile_id and security in (None, "auto"):
            _, key_mgmt, _ = self._read_nmcli([
                "-g", "802-11-wireless-security.key-mgmt", "con", "show", "uuid", profile_id,
            ])
            if key_mgmt.strip() == "sae":
                security = "wpa3"
            elif key_mgmt.strip() not in ("", "wpa-psk"):
                return False, "Only personal WPA networks support password replacement"
        stage_name = self._stage_profile_name()
        self._staged_profile_name = stage_name
        self._save_runtime_state(self._load_saved_device_autoconnect())
        created, result = wifi_manager.add_wifi_profile(
            stage_name,
            ssid,
            self.interface,
            secret=secret,
            hidden=hidden,
            autoconnect=False,
            security=security,
            managed_stage=True,
            runner=self._nm_runner,
        )
        if not created:
            self.discard_staged_connection()
            return False, result
        ok, message = wifi_manager.activate_connection(stage_name, interface=self.interface, runner=self._nm_runner)
        if not ok:
            self.discard_staged_connection()
            return False, message
        return True, f"Connected to {ssid}"

    def finalize_staged_connection(self, replaced_profile_id: str | None = None) -> str | None:
        warnings: list[str] = []
        if self._staged_profile_name:
            ok, message = self._run_nmcli([
                "con", "modify", "id", self._staged_profile_name,
                "connection.autoconnect", "yes",
                "user.data", "org.cts-scoreboard.role=ready",
            ])
            if not ok:
                raise BackendError(message)
        if replaced_profile_id:
            ok, message = wifi_manager.set_autoconnect(replaced_profile_id, False, runner=self._nm_runner)
            if not ok:
                warnings.append(message)
            ok, message = wifi_manager.forget(replaced_profile_id, runner=self._nm_runner)
            if not ok:
                warnings.append(message)
        self._staged_profile_name = None
        self._save_runtime_state(self._load_saved_device_autoconnect())
        if warnings:
            return self._sanitize_command_error("; ".join(warnings))
        return None

    def staged_profile_id(self) -> str | None:
        if self._staged_profile_name is None:
            return None
        value = wifi_manager.show_connection(self._staged_profile_name, runner=self._read_nmcli).get("id")
        return value if isinstance(value, str) else None

    def _cleanup_interrupted_profiles(self) -> None:
        for profile in wifi_manager.list_connection_profiles(runner=self._read_nmcli):
            name = profile.get("name")
            if not isinstance(name, str) or not name.startswith(STAGED_PROFILE_PREFIX):
                continue
            if profile.get("device") not in (None, "", self.interface):
                continue
            _, role, _ = self._read_nmcli(["-g", "user.data", "con", "show", "uuid", profile["id"]])
            tags = [item.partition("=") for item in role.strip().split(",")]
            if not any(key.strip() == "org.cts-scoreboard.role" and value.strip() == "staging"
                       for key, separator, value in tags if separator):
                continue
            ok, message = wifi_manager.forget(profile["id"], runner=self._nm_runner)
            if not ok:
                raise BackendError(f"Cannot remove interrupted join profile: {message}")
        self._staged_profile_name = None
        self._save_runtime_state(self._load_saved_device_autoconnect())

    def discard_staged_connection(self) -> None:
        stage_name = self._staged_profile_name
        if stage_name:
            ok, message = wifi_manager.forget(stage_name, runner=self._nm_runner)
            if not ok and "unknown connection" not in message.lower():
                raise BackendError(f"Failed to remove staged credentials: {message}")
            self._staged_profile_name = None
            self._save_runtime_state(self._load_saved_device_autoconnect())

    def forget(self, ssid: str, profile_id: str | None = None) -> tuple[bool, str]:
        return wifi_manager.forget(profile_id or ssid, runner=self._nm_runner)

    def update_password(self, ssid: str, secret: str, profile_id: str | None = None) -> tuple[bool, str]:
        return wifi_manager.update_password(profile_id or ssid, secret, runner=self._nm_runner)

    def activate_setup(self) -> None:
        self._ensure_runtime_dir()
        self._validate_radio()
        self._ap_address = self._select_ap_address()
        try:
            self._ensure_setup_autoconnect_inhibited()
            self._ensure_ap_profile()
            ok, message = wifi_manager.activate_connection(
                self._ap_uuid,
                interface=self.interface,
                runner=self._nm_runner,
            )
            if not ok:
                raise BackendError(message)
            self._await(
                lambda: bool(self.snapshot().get("setup_active")),
                timeout=15.0,
                error_message="AP did not become active",
            )
            self._start_dnsmasq()
            self._install_nft()
        except BackendError as error:
            cleanup_errors = self._rollback_failed_setup()
            raise self._combined_backend_error(error, cleanup_errors) from error

    def deactivate_setup(self, *, restore_autoconnect: bool = False) -> None:
        self._ensure_setup_autoconnect_inhibited()
        self._stop_dnsmasq(ignore_missing=True)
        self._remove_nft(ignore_missing=True)
        self._deactivate_ap_connection(ignore_inactive=True)
        if restore_autoconnect:
            self.restore_device_autoconnect()

    def restore_setup(self) -> None:
        self.activate_setup()

    def managed_ap_profile_id(self) -> str | None:
        return self._ap_uuid

    def restore_device_autoconnect(self, ignore_missing: bool = False) -> None:
        saved = self._load_saved_device_autoconnect()
        if saved is None:
            if ignore_missing:
                return
            raise BackendError("No saved device autoconnect policy")
        ok, message = wifi_manager.set_device_autoconnect(self.interface, saved, runner=self._nm_runner)
        if not ok:
            raise BackendError(message)
        self._save_runtime_state(None)

    def _ensure_setup_autoconnect_inhibited(self, default_restore: bool | None = None) -> None:
        saved = self._load_saved_device_autoconnect()
        if saved is None:
            current = wifi_manager.get_device_autoconnect(self.interface, runner=self._nm_runner)
            if current is None:
                raise BackendError("Cannot read NetworkManager device autoconnect policy")
            else:
                saved_value = current
            self._save_runtime_state(saved_value)
        ok, message = wifi_manager.set_device_autoconnect(self.interface, False, runner=self._nm_runner)
        if not ok:
            raise BackendError(message)

    def _ap_profile(self) -> dict[str, Any]:
        _, output, _ = self._read_nmcli(["-g", "UUID", "con", "show"])
        if self._ap_uuid not in output.splitlines():
            return {}
        profile = wifi_manager.show_connection(self._ap_uuid, runner=self._read_nmcli)
        if (profile.get("name") != self.ap_connection_id
                or profile.get("ssid") != self.ap_ssid or profile.get("mode") != "ap"):
            raise BackendError("Managed AP UUID belongs to an unexpected profile")
        return profile

    def _read_nmcli(self, args: list[str], timeout: int = 30) -> tuple[int, str, str]:
        result = self._nm_runner(args, timeout=min(timeout, 10))
        if result[0] != 0:
            raise BackendError(self._sanitize_command_error(result[2] or "NetworkManager query failed"))
        return result

    def _validate_radio(self) -> None:
        _, output, _ = self._read_nmcli([
            "-g", "GENERAL.TYPE,GENERAL.NM-MANAGED,GENERAL.STATE,WIFI-PROPERTIES.AP",
            "dev", "show", self.interface,
        ])
        values = output.splitlines()
        if len(values) != 4 or values[0] != "wifi" or values[1] != "yes":
            raise BackendError(f"{self.interface} must be a NetworkManager-managed Wi-Fi interface")
        if values[3] != "yes":
            raise BackendError(f"{self.interface} does not support access-point mode")
        if values[2].startswith(("10 ", "20 ")):
            raise BackendError(f"{self.interface} is unavailable; check rfkill and the Wi-Fi country setting")

    def _active_wifi_connection(self) -> dict[str, Any] | None:
        active_by_device = {
            item.get("device"): item
            for item in wifi_manager.list_active_connections(runner=self._read_nmcli)
            if item.get("device")
        }
        active = active_by_device.get(self.interface)
        if not active:
            return None
        info = wifi_manager.show_connection(active["id"], runner=self._read_nmcli)
        if not info:
            return None
        return info

    def _usable_ipv4(
        self,
        device: str,
        *,
        excluded_networks: list[ipaddress.IPv4Network] | None = None,
    ) -> str | None:
        for address_text in wifi_manager.get_device_ipv4_addresses(device, runner=self._read_nmcli):
            address = ipaddress.ip_address(address_text)
            if not isinstance(address, ipaddress.IPv4Address):
                continue
            if address.is_loopback or address.is_link_local or address.is_unspecified:
                continue
            if excluded_networks and any(address in network for network in excluded_networks):
                continue
            return str(address)
        return None

    def _ethernet_connected(self, *, excluded_networks: list[ipaddress.IPv4Network]) -> bool:
        return self._ethernet_ipv4(excluded_networks=excluded_networks) is not None

    def _ethernet_ipv4(self, *, excluded_networks: list[ipaddress.IPv4Network]) -> str | None:
        for device in wifi_manager.list_device_statuses(runner=self._read_nmcli):
            if device.get("type") != "ethernet" or device.get("state") != "connected":
                continue
            address = self._usable_ipv4(device["device"], excluded_networks=excluded_networks)
            if address:
                return address
        return None

    def _connectivity(self) -> str:
        result = self._command(["nmcli", "-t", "-f", "CONNECTIVITY", "general", "status"], timeout=15)
        if result.returncode != 0:
            return "unknown"
        value = result.stdout.strip().splitlines()
        text = value[-1].strip().lower() if value else "unknown"
        return text if text in {"full", "limited", "portal", "none"} else "unknown"

    def _ensure_ap_profile(self) -> None:
        existing = self._ap_profile()
        if existing:
            args = ["con", "modify", "uuid", self._ap_uuid]
        else:
            args = [
                "con",
                "add",
                "type",
                "wifi",
                "ifname",
                self.interface,
                "con-name",
                self.ap_connection_id,
                "ssid",
                self.ap_ssid,
                "connection.uuid", self._ap_uuid,
                "user.data", "org.cts-scoreboard.role=setup",
            ]
        args += [
                "802-11-wireless.mode",
                "ap",
                "connection.autoconnect",
                "no",
                "connection.interface-name",
                self.interface,
                "ipv4.method",
                "manual",
                "ipv4.addresses",
                self._ap_address,
                "ipv4.never-default",
                "yes",
                "ipv6.method",
                "disabled",
                "802-11-wireless.hidden",
                "no",
            ]
        ok, message = self._run_nmcli(args)
        if not ok:
            raise BackendError(message)

    def _dnsmasq_command(self) -> list[str]:
        ap_interface = ipaddress.ip_interface(self._ap_address)
        network = ap_interface.network
        if not isinstance(network, ipaddress.IPv4Network) or network.prefixlen != 24:
            raise BackendError("AP subnet must be an IPv4 /24")
        hosts = list(network.hosts())
        if ap_interface.ip in hosts[9:-9]:
            raise BackendError("AP gateway must be outside the DHCP lease pool")
        if len(hosts) < 20:
            raise BackendError("AP subnet is too small")
        range_start = str(hosts[9])
        range_end = str(hosts[-10])
        gateway = str(ap_interface.ip)
        return [
            "dnsmasq",
            "--conf-file=/dev/null",
            "--keep-in-foreground",
            "--user=root",
            "--bind-interfaces",
            "--interface",
            self.interface,
            "--except-interface=lo",
            "--listen-address",
            gateway,
            "--no-hosts",
            "--no-resolv",
            "--dhcp-authoritative",
            "--dhcp-range",
            f"{range_start},{range_end},{network.netmask},12h",
            "--dhcp-option=option:router,{}".format(gateway),
            "--dhcp-option=option:dns-server,{}".format(gateway),
            "--address=/#/{}".format(gateway),
            "--pid-file",
            self._runtime_path(DNSMASQ_PID_FILE_NAME),
            "--dhcp-leasefile",
            self._runtime_path(DNSMASQ_LEASE_FILE_NAME),
        ]

    def _start_dnsmasq(self) -> None:
        if self._dnsmasq_process is not None and self._dnsmasq_process.poll() is None:
            return
        self._stop_dnsmasq(ignore_missing=True)
        try:
            process = self._popen_factory(
                self._dnsmasq_command(),
                stdout=subprocess.DEVNULL,
                stderr=None,
                text=True,
            )
        except OSError as error:
            raise BackendError(f"Cannot start setup dnsmasq: {error}") from error
        self._dnsmasq_process = process
        self._sleeper(0.2)
        if process.poll() is not None:
            stderr = ""
            if process.stderr is not None:
                stderr = process.stderr.read() or ""
            self._dnsmasq_process = None
            raise BackendError(self._sanitize_command_error(stderr or "dnsmasq failed to start; see the controller journal"))

    def _stop_dnsmasq(self, *, ignore_missing: bool) -> None:
        process = self._dnsmasq_process
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            self._dnsmasq_process = None
        pid_path = self._runtime_path(DNSMASQ_PID_FILE_NAME)
        try:
            with open(pid_path, "r", encoding="utf-8") as handle:
                pid = int(handle.read().strip())
        except FileNotFoundError:
            pid = None
        except (OSError, ValueError) as error:
            raise BackendError("Cannot read setup dnsmasq PID") from error
        if pid is not None:
            # A stale PID may have been reused by an unrelated process.
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as handle:
                    argv = handle.read().split(b"\0")
            except FileNotFoundError:
                argv = []
            except OSError as error:
                raise BackendError("Cannot identify stale setup dnsmasq") from error
            if argv and (os.path.basename(os.fsdecode(argv[0])) != "dnsmasq"
                         or os.fsencode(pid_path) not in argv):
                raise BackendError("Setup dnsmasq PID belongs to a different process")
            if argv:
                try:
                    os.kill(pid, signal.SIGTERM)
                    deadline = time.monotonic() + 3
                    while os.path.exists(f"/proc/{pid}") and time.monotonic() < deadline:
                        self._sleeper(0.1)
                    if os.path.exists(f"/proc/{pid}"):
                        os.kill(pid, signal.SIGKILL)
                        self._await(lambda: not os.path.exists(f"/proc/{pid}"), timeout=3,
                                    error_message="Stale setup dnsmasq did not stop")
                except ProcessLookupError:
                    pass
                except OSError as error:
                    raise BackendError("Cannot stop stale setup dnsmasq") from error
        for path in (pid_path, self._runtime_path(DNSMASQ_LEASE_FILE_NAME)):
            try:
                os.unlink(path)
            except FileNotFoundError:
                continue

    def _install_nft(self) -> None:
        self._remove_nft(ignore_missing=True)
        rules = self._nft_rules()
        result = self._command(["nft", "-f", "-"], timeout=15, input_text=rules)
        if result.returncode != 0:
            raise BackendError(self._sanitize_command_error(result.stderr or result.stdout or "nft failed"))

    def _remove_nft(self, *, ignore_missing: bool) -> None:
        result = self._command(["nft", "delete", "table", "inet", NFT_TABLE_NAME], timeout=15)
        if result.returncode == 0:
            return
        text = (result.stderr or result.stdout or "").lower()
        if ignore_missing and ("no such file" in text or "not found" in text):
            return
        raise BackendError(self._sanitize_command_error(result.stderr or result.stdout or "nft cleanup failed"))

    def _nft_rules(self) -> str:
        return "\n".join(
            [
                f"add table inet {NFT_TABLE_NAME}",
                f"add chain inet {NFT_TABLE_NAME} prerouting {{ type nat hook prerouting priority dstnat; policy accept; }}",
                (
                    f"add rule inet {NFT_TABLE_NAME} prerouting iifname \"{self.interface}\" "
                    f"ip saddr {ipaddress.ip_interface(self._ap_address).network} tcp dport 80 redirect to :{self.ap_port}"
                ),
                f"add chain inet {NFT_TABLE_NAME} forward {{ type filter hook forward priority filter; policy accept; }}",
                f"add rule inet {NFT_TABLE_NAME} forward iifname \"{self.interface}\" drop",
                f"add rule inet {NFT_TABLE_NAME} forward oifname \"{self.interface}\" drop",
                "",
            ]
        )

    def _select_ap_address(self) -> str:
        configured = ipaddress.ip_interface(self._configured_ap_address)
        candidates = [configured]
        active = self._active_wifi_connection()
        if active and active.get("id") == self._ap_uuid and active.get("mode") == "ap":
            addresses = wifi_manager.get_device_ipv4_addresses(self.interface, runner=self._read_nmcli)
            if addresses:
                self._ap_address = f"{addresses[0]}/24"
                candidates.insert(0, ipaddress.ip_interface(self._ap_address))
        for cidr in ("192.168.5.1/24", "192.168.6.1/24", "10.43.0.1/24", "10.44.0.1/24"):
            candidate = ipaddress.ip_interface(cidr)
            if candidate != configured:
                candidates.append(candidate)
        conflicts = self._route_networks()
        for candidate in candidates:
            if not any(candidate.network.overlaps(route) for route in conflicts):
                return str(candidate)
        raise BackendError("No nonconflicting setup subnet is available")

    def _route_networks(self) -> list[ipaddress.IPv4Network]:
        result = self._command(["ip", "-4", "route", "show"], timeout=15)
        if result.returncode != 0:
            raise BackendError("Cannot inspect local IPv4 routes")
        routes: list[ipaddress.IPv4Network] = []
        active = self._active_wifi_connection()
        own_ap = active is not None and active.get("id") == self._ap_uuid and active.get("mode") == "ap"
        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            first = line.split()[0]
            if first == "default":
                continue
            if own_ap and f"dev {self.interface}" in line and first == str(ipaddress.ip_interface(self._ap_address).network):
                continue
            try:
                network = ipaddress.ip_network(first, strict=False)
            except ValueError:
                continue
            if isinstance(network, ipaddress.IPv4Network):
                routes.append(network)
        return routes

    def _stage_profile_name(self) -> str:
        return f"{STAGED_PROFILE_PREFIX}{uuid.uuid4()}"

    def _ensure_runtime_dir(self) -> None:
        try:
            os.makedirs(self.runtime_dir, mode=0o750, exist_ok=True)
            info = os.lstat(self.runtime_dir)
        except OSError as error:
            raise BackendError(f"Runtime directory error: {error}") from error
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise BackendError("Runtime directory must be a real directory")
        if os.geteuid() == 0 and info.st_uid != 0:
            raise BackendError("Runtime directory must be owned by root")
        try:
            os.chmod(self.runtime_dir, 0o750)
            if self.runtime_group:
                gid = grp.getgrnam(self.runtime_group).gr_gid
                owner_uid = 0 if os.geteuid() == 0 else info.st_uid
                os.chown(self.runtime_dir, owner_uid, gid)
        except (KeyError, OSError) as error:
            raise BackendError(f"Runtime directory error: {error}") from error

    def _runtime_path(self, name: str) -> str:
        return os.path.join(self.runtime_dir, name)

    def _load_saved_device_autoconnect(self) -> bool | None:
        state = self._load_runtime_state()
        value = state.get("device_autoconnect")
        return value if isinstance(value, bool) else None

    def _load_runtime_state(self) -> dict[str, Any]:
        path = self._runtime_path(STATE_FILE_NAME)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                value = json.load(handle)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as error:
            raise BackendError(f"Cannot read controller runtime state: {error}") from error
        if not isinstance(value, dict):
            raise BackendError("Invalid controller runtime state")
        return value

    def _save_runtime_state(self, autoconnect: bool | None) -> None:
        path = self._runtime_path(STATE_FILE_NAME)
        if autoconnect is None and self._staged_profile_name is None:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            return
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump({"device_autoconnect": autoconnect, "staged_profile": self._staged_profile_name}, handle)
        os.replace(temporary, path)

    def _await(
        self,
        predicate: Callable[[], bool],
        *,
        timeout: float,
        error_message: str,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            self._sleeper(0.2)
        raise BackendError(error_message)

    def _sanitize_command_error(self, text: str) -> str:
        cleaned = " ".join((text or "").split())
        return cleaned[:240] or "Provisioning command failed"

    def _rollback_failed_setup(self) -> list[str]:
        errors: list[str] = []
        try:
            self._stop_dnsmasq(ignore_missing=True)
        except BackendError as error:
            errors.append(f"dnsmasq cleanup failed: {error}")
        try:
            self._remove_nft(ignore_missing=True)
        except BackendError as error:
            errors.append(f"nft cleanup failed: {error}")
        try:
            self._deactivate_ap_connection(ignore_inactive=True)
        except BackendError as error:
            errors.append(f"AP cleanup failed: {error}")
        if not errors:
            try:
                self.restore_device_autoconnect()
            except BackendError as error:
                errors.append(f"autoconnect restore failed: {error}")
        return errors

    def _deactivate_ap_connection(self, *, ignore_inactive: bool) -> None:
        if not self._ap_profile():
            return
        active = self._active_wifi_connection()
        if not active or active.get("id") != self._ap_uuid:
            return
        ok, message = wifi_manager.deactivate_connection(self._ap_uuid, runner=self._nm_runner)
        if not ok and not (ignore_inactive and "not an active connection" in message.lower()):
            raise BackendError(message)
        self._await(
            lambda: not bool(self.snapshot().get("setup_active")),
            timeout=10.0,
            error_message="AP did not shut down cleanly",
        )

    def _combined_backend_error(self, primary: BackendError, cleanup_errors: list[str]) -> BackendError:
        if not cleanup_errors:
            return BackendError(str(primary))
        return BackendError(f"{primary}; cleanup: {'; '.join(cleanup_errors)}")

    def _run_nmcli(
        self,
        args: list[str],
        *,
        ignore_already_exists: bool = False,
    ) -> tuple[bool, str]:
        code, out, err = wifi_manager._call_runner(self._nm_runner, args)
        if code == 0:
            return True, out.strip()
        message = self._sanitize_command_error(err or out)
        if ignore_already_exists and "already exists" in message.lower():
            return True, message
        return False, message

    def _default_command_runner(
        self,
        argv: list[str],
        *,
        timeout: int,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )

    def _command(
        self, argv: list[str], *, timeout: int, input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return self._run_command(argv, timeout=timeout, input_text=input_text)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BackendError(f"{argv[0]} failed or timed out") from error

    def _has_binary(self, name: str) -> bool:
        return any(
            os.path.isfile(os.path.join(prefix, name))
            for prefix in (
                "/usr/bin",
                "/usr/sbin",
                "/bin",
                "/sbin",
            )
        ) or any(
            os.path.isfile(os.path.join(prefix, name))
            for prefix in os.getenv("PATH", "").split(os.pathsep)
            if prefix
        )


class ProvisioningController:
    def __init__(
        self,
        config: ProvisioningConfig | None = None,
        backend: Any | None = None,
        clock: Any | None = None,
        start_worker: bool = True,
    ):
        self.config = config or ProvisioningConfig()
        self.backend = backend or SystemProvisioningBackend(
            self.config.interface,
            self.config.ap_connection_id,
            ap_ssid=self.config.ap_ssid,
            ap_address=self.config.ap_address,
            ap_port=self.config.ap_port,
            runtime_dir=os.path.dirname(self.config.socket_path),
            runtime_group=self.config.socket_group,
        )
        self.clock = clock or _DefaultClock()
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._network_lock = threading.RLock()
        self._stop = False
        self._closed = False
        self._started_at = self.clock.monotonic()
        self._disconnect_since: float | None = None
        self._retry_not_before = 0.0
        self._ever_connected = False
        self._counter = 0
        self._scan_cache: list[dict[str, Any]] = []
        self._scan_at = 0.0
        self._saved_cache: list[str] = []
        self._profiles_cache: list[dict[str, Any]] = []
        self._metadata_at = 0.0
        self._pending_join: PendingJoin | None = None
        self._queued_operation: Operation | None = None
        self._active_operation: Operation | None = None
        self._sync_operation: str | None = None
        self._status = self._status_from_snapshot(
            {
                "hostname": socket.gethostname(),
                "local_url": self._local_url(),
                "setup_url": self._setup_url(self.config.ap_address),
                "ap_network": self._ap_network(self.config.ap_address),
                "connectivity": "unknown",
            },
            state=STATE_STARTING,
            message="Waiting for NetworkManager.",
            setup_active=False,
            available=self._backend_available(),
            operation_id=None,
        )
        self._worker = threading.Thread(target=self._worker_loop, name="wifi-provisioning-worker", daemon=True)
        self._monitor = threading.Thread(target=self._monitor_loop, name="wifi-provisioning-monitor", daemon=True)
        self.reconcile()
        if start_worker:
            self._worker.start()
            self._monitor.start()

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._stop = True
            self._queued_operation = None
            self._condition.notify_all()
        if self._worker.is_alive():
            self._worker.join()
        if self._monitor.is_alive():
            self._monitor.join()
        shutdown = getattr(self.backend, "shutdown", None)
        if callable(shutdown):
            with self._network_lock:
                shutdown()
        self._closed = True

    def reconcile(self) -> dict[str, Any]:
        snapshot: dict[str, Any]
        try:
            cleanup = getattr(self.backend, "startup_cleanup", None)
            if callable(cleanup):
                snapshot = cleanup()
            else:
                snapshot = self._snapshot()
        except BackendError as error:
            with self._lock:
                self._status = self._status_from_snapshot(
                    self._snapshot_defaults(),
                    state=STATE_ERROR,
                    message=str(error),
                    setup_active=False,
                    available=self._backend_available(),
                    operation_id=None,
                )
            return self.status()
        self._refresh_metadata()
        self._apply_idle_snapshot(snapshot, message=self._default_message_for_snapshot(snapshot))
        if self._is_connected_snapshot(snapshot):
            self._ever_connected = True
            self._disconnect_since = None
        return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._expire_pending_join_locked()
            self._status["scan_stale"] = self._scan_is_stale_locked()
            return dict(self._status)

    def flush(self, timeout: float = 5.0) -> bool:
        deadline = self.clock.monotonic() + timeout
        with self._condition:
            while self._busy_locked() and self.clock.monotonic() < deadline:
                self._condition.wait(timeout=0.05)
            return not self._busy_locked()

    def poll_once(self) -> None:
        self._monitor_once()

    def request(self, request: dict[str, Any]) -> dict[str, Any]:
        command = request.get("command")
        params = request.get("params")
        if not isinstance(command, str) or not command:
            return self._failure("Invalid command", self.status())
        if not isinstance(params, dict):
            return self._failure("Invalid params", self.status())
        return self.handle(command, params)

    def handle(self, command: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        handlers = {
            "status": self._handle_status,
            "scan": self._handle_scan,
            "refresh": self._handle_refresh,
            "prepare_join": self._handle_prepare_join,
            "commit_join": self._handle_commit_join,
            "cancel_join": self._handle_cancel_join,
            "retry_saved": self._handle_retry_saved,
            "enter_setup": self._handle_enter_setup,
            "forget": self._handle_forget,
            "update_password": self._handle_update_password,
        }
        handler = handlers.get(command)
        if handler is None:
            return self._failure("Unsupported command", self.status())
        allowed = {
            "status": set(), "scan": set(), "refresh": {"confirmed"},
            "prepare_join": {"ssid", "password", "hidden", "security", "profile_id"},
            "commit_join": {"operation_id"}, "cancel_join": {"operation_id"},
            "retry_saved": set(), "enter_setup": set(),
            "forget": {"ssid", "profile_id"},
            "update_password": {"ssid", "profile_id", "password"},
        }
        if params is not None and (not isinstance(params, dict) or set(params) - allowed[command]):
            return self._failure("Unexpected Wi-Fi parameter", self.status())
        if self._stop:
            return self._failure("Wi-Fi controller is stopping", self.status())
        return handler(params or {})

    def _monitor_loop(self) -> None:
        while True:
            with self._condition:
                if self._stop:
                    return
            self._monitor_once()
            self.clock.sleep(MONITOR_INTERVAL)

    def _monitor_once(self) -> None:
        if not self._network_lock.acquire(blocking=False):
            return
        try:
            self._poll_network()
        except BackendError as error:
            with self._lock:
                self._status.update(state=STATE_ERROR, message=str(error))
        finally:
            self._network_lock.release()

    def _poll_network(self) -> None:
        if self._backend_available() is False:
            with self._lock:
                self._status.update(
                    state=STATE_ERROR, available=False,
                    message="Wi-Fi provisioning dependencies are unavailable; check the controller installation.",
                )
            return
        if self._operation_in_flight():
            return
        snapshot = self._snapshot()
        if not snapshot:
            return
        now = self.clock.monotonic()
        connected = self._is_connected_snapshot(snapshot)
        if snapshot.get("setup_active"):
            self._disconnect_since = None
            if snapshot.get("setup_healthy") is False:
                operation = Operation(
                    self._next_operation_id("recover"), STATE_RECOVERING, "enter_setup",
                    {}, self._run_enter_setup, now,
                )
                self._queue_operation(operation, message="Restoring setup DHCP/DNS service.")
                return
            self._apply_idle_snapshot(snapshot, message=self._status.get("message") or self._default_message_for_snapshot(snapshot))
            return
        if connected:
            self._ever_connected = True
            self._disconnect_since = None
            self._apply_idle_snapshot(snapshot, message=self._default_message_for_snapshot(snapshot))
            return
        if self._disconnect_since is None:
            self._disconnect_since = now
        boot_elapsed = now - self._started_at
        idle_elapsed = now - self._disconnect_since
        if not self._ever_connected and boot_elapsed < self.config.boot_grace:
            self._apply_idle_snapshot(snapshot, state=STATE_STARTING, message="Waiting for saved networks.")
            return
        if idle_elapsed < self.config.disconnect_grace and self._ever_connected:
            self._apply_idle_snapshot(snapshot, state=STATE_NORMAL, message="Waiting for Wi-Fi to recover.")
            return
        if now >= self._retry_not_before:
            operation = Operation(
                id=self._next_operation_id("setup"),
                state=STATE_SETUP,
                command="enter_setup",
                params={"automatic": True},
                func=self._run_enter_setup,
                queued_at=now,
            )
            queued = self._queue_operation(operation, message="Starting Wi-Fi setup.")
            if queued:
                return

    def _backend_available(self) -> bool:
        probe = getattr(self.backend, "available", None)
        if not callable(probe):
            return True
        return bool(probe())

    def _snapshot(self) -> dict[str, Any]:
        value = self.backend.snapshot()
        return value if isinstance(value, dict) else {}

    def _refresh_metadata(self) -> None:
        profiles: Any = getattr(self.backend, "profiles", lambda: [])()
        saved: Any = getattr(self.backend, "saved_networks", lambda: [])()
        if not isinstance(profiles, list):
            profiles = []
        if not isinstance(saved, list):
            saved = []
        with self._lock:
            self._profiles_cache = [profile for profile in profiles if isinstance(profile, dict)]
            self._saved_cache = [item for item in saved if isinstance(item, str)]
            self._metadata_at = self.clock.monotonic()

    def _handle_status(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._success("", self.status())

    def _handle_scan(self, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            return self._success(
                "",
                self.status(),
                networks=list(self._scan_cache),
                saved=list(self._saved_cache),
                profiles=list(self._profiles_cache),
            )

    def _handle_refresh(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("confirmed") is not True:
            return self._failure("Refresh requires confirmation", self.status())
        operation = Operation(
            id=self._next_operation_id("refresh"),
            state=STATE_REFRESHING,
            command="refresh",
            params={"confirmed": True},
            func=self._run_refresh,
            queued_at=self.clock.monotonic(),
        )
        if not self._queue_operation(operation, message="Refreshing Wi-Fi network list."):
            return self._busy_response()
        return self._success("Refresh queued", self.status(), operation_id=operation.id)

    def _handle_prepare_join(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            ssid = self._normalize_ssid(params.get("ssid"))
            password = self._normalize_optional_password(params, required=False)
            hidden = self._normalize_bool(params.get("hidden"))
            security = self._normalize_security(params.get("security"))
            profile_id = self._normalize_profile_id(params.get("profile_id"))
            if profile_id:
                profile_id = self._saved_profile_id(ssid, profile_id)
        except ValueError as error:
            return self._failure(str(error), self.status())
        managed_ap_id = self._managed_ap_profile_id()
        if ssid in {self.config.ap_ssid, self.config.ap_connection_id} or (managed_ap_id and profile_id == managed_ap_id):
            return self._failure("Managed AP cannot be joined as upstream", self.status())
        if profile_id is None and password is None and self._security_needs_password(security):
            return self._failure("Password required for that network", self.status())
        if security == "open" and password is not None:
            return self._failure("Open networks do not use a password", self.status())
        with self._lock:
            self._expire_pending_join_locked()
            prepared = {
                "ssid": ssid,
                "password": password,
                "hidden": hidden,
                "security": security,
                "profile_id": profile_id,
            }
            if self._pending_join and self._pending_join_matches_locked(prepared):
                return self._success("Join already prepared", self.status(), operation_id=self._pending_join.operation_id)
            if self._busy_locked() or self._pending_join is not None:
                return self._busy_response_locked()
            operation_id = self._next_operation_id("join")
            self._pending_join = PendingJoin(
                operation_id=operation_id,
                ssid=ssid,
                password=password,
                hidden=hidden,
                security=security,
                profile_id=profile_id,
                prepared_at=self.clock.monotonic(),
            )
            self._status.update(
                {
                    "state": STATE_JOIN_PENDING,
                    "operation_id": operation_id,
                    "last_operation": None,
                    "target_ssid": ssid,
                    "message": f"Prepared join for {ssid}",
                    "scan_stale": self._scan_is_stale_locked(),
                }
            )
            return self._success("Join prepared", dict(self._status), operation_id=operation_id)

    def _handle_commit_join(self, params: dict[str, Any]) -> dict[str, Any]:
        operation_id = params.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id.strip():
            return self._failure("operation_id is required", self.status())
        with self._lock:
            self._expire_pending_join_locked()
            pending = self._pending_join
            if pending is None:
                completed = self._status.get("last_operation")
                if isinstance(completed, dict) and completed.get("id") == operation_id.strip():
                    return self._success("Join already processed", self.status(), operation_id=operation_id.strip())
                return self._failure("No pending join", self.status())
            if pending.operation_id != operation_id.strip():
                return self._failure("Unknown join operation", self.status())
            if pending.committed:
                return self._success("Join already committed", self.status(), operation_id=pending.operation_id)
            if self._busy_locked():
                return self._busy_response_locked()
            pending.committed = True
            operation = Operation(
                id=pending.operation_id,
                state=STATE_JOINING,
                command="commit_join",
                params={"operation_id": pending.operation_id},
                func=self._build_join_runner(pending),
                queued_at=self.clock.monotonic(),
            )
            self._queue_operation_locked(operation, message=f"Joining {pending.ssid}.")
            return self._success("Join committed", dict(self._status), operation_id=operation.id)

    def _build_join_runner(self, pending: PendingJoin) -> Callable[[], dict[str, Any]]:
        def run() -> dict[str, Any]:
            return self._run_join(pending)

        return run

    def _handle_cancel_join(self, params: dict[str, Any]) -> dict[str, Any]:
        operation_id = params.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id.strip():
            return self._failure("operation_id is required", self.status())
        with self._lock:
            self._expire_pending_join_locked()
            pending = self._pending_join
            if pending is None:
                return self._success("No pending join", self.status())
            if pending.operation_id != operation_id.strip():
                return self._failure("Unknown join operation", self.status())
            if pending.committed:
                return self._failure("Join already committed", self.status())
            self._pending_join = None
            snapshot = self._snapshot_defaults()
            setup_active = bool(self._status.get("setup_active"))
            state = STATE_SETUP if setup_active else STATE_NORMAL
            self._status = self._status_from_snapshot(
                snapshot,
                state=state,
                message="Join cancelled",
                setup_active=setup_active,
                available=self._backend_available(),
                operation_id=None,
                current=self._status,
            )
            return self._success("Join cancelled", dict(self._status))

    def _handle_retry_saved(self, params: dict[str, Any]) -> dict[str, Any]:
        operation = Operation(
            id=self._next_operation_id("retry"),
            state=STATE_JOINING,
            command="retry_saved",
            params={},
            func=self._run_retry_saved,
            queued_at=self.clock.monotonic(),
        )
        if not self._queue_operation(operation, message="Retrying saved Wi-Fi networks."):
            return self._busy_response()
        return self._success("Saved-network retry queued", self.status(), operation_id=operation.id)

    def _handle_enter_setup(self, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._status.get("setup_active") and self._status.get("state") in {STATE_SETUP, STATE_RECOVERING, STATE_ERROR}:
                return self._success("Setup is already active", self.status())
        operation = Operation(
            id=self._next_operation_id("setup"),
            state=STATE_SETUP,
            command="enter_setup",
            params={},
            func=self._run_enter_setup,
            queued_at=self.clock.monotonic(),
        )
        if not self._queue_operation(operation, message="Starting Wi-Fi setup."):
            return self._busy_response()
        return self._success("Setup queued", self.status(), operation_id=operation.id)

    def _handle_forget(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            ssid = self._normalize_ssid(params.get("ssid"))
            profile_id = self._saved_profile_id(ssid, self._normalize_profile_id(params.get("profile_id")))
        except ValueError as error:
            return self._failure(str(error), self.status())
        managed_ap_id = self._managed_ap_profile_id()
        if ssid == self.config.ap_ssid or profile_id == self.config.ap_connection_id or (managed_ap_id and profile_id == managed_ap_id):
            return self._failure("Managed AP profile cannot be modified", self.status())
        return self._run_serial_mutation(
            "forget",
            lambda: self._forget_network(ssid, profile_id),
        )

    def _handle_update_password(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            ssid = self._normalize_ssid(params.get("ssid"))
            password = self._normalize_optional_password(params, required=True)
            profile_id = self._saved_profile_id(ssid, self._normalize_profile_id(params.get("profile_id")))
        except ValueError as error:
            return self._failure(str(error), self.status())
        managed_ap_id = self._managed_ap_profile_id()
        if ssid == self.config.ap_ssid or profile_id == self.config.ap_connection_id or (managed_ap_id and profile_id == managed_ap_id):
            return self._failure("Managed AP profile cannot be modified", self.status())
        return self._run_serial_mutation(
            "update_password",
            lambda: self._update_password(ssid, password or "", profile_id),
        )

    def _run_serial_mutation(self, name: str, func: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        operation = Operation(self._next_operation_id(name), STATE_REFRESHING,
                              name, {}, func, self.clock.monotonic())
        if not self._queue_operation(operation, message="Updating saved Wi-Fi profile."):
            return self._busy_response()
        return self._success("Profile update queued", self.status(), operation_id=operation.id)

    def _forget_network(self, ssid: str, profile_id: str | None) -> dict[str, Any]:
        ok, message = self.backend.forget(ssid, profile_id=profile_id)
        self._refresh_metadata()
        if ok:
            with self._lock:
                self._scan_cache = [network for network in self._scan_cache if network.get("ssid") != ssid]
            return self._success(message, self._idle_status_for_snapshot(self._snapshot(), message=message))
        return self._failure(message, self._idle_status_for_snapshot(self._snapshot(), message=message))

    def _update_password(self, ssid: str, password: str, profile_id: str | None) -> dict[str, Any]:
        ok, message = self.backend.update_password(ssid, password, profile_id=profile_id)
        self._refresh_metadata()
        if ok:
            return self._success(message, self._idle_status_for_snapshot(self._snapshot(), message=message))
        return self._failure(message, self._idle_status_for_snapshot(self._snapshot(), message=message))

    def _worker_loop(self) -> None:
        while True:
            with self._condition:
                while not self._stop and self._queued_operation is None:
                    self._condition.wait(timeout=0.1)
                if self._stop:
                    return
                operation = self._queued_operation
                self._queued_operation = None
                self._active_operation = operation
            assert operation is not None
            try:
                with self._network_lock:
                    result = operation.func()
            except BackendError as error:
                result = self._operation_failure(operation.state, str(error))
            except (OSError, subprocess.SubprocessError) as error:
                result = self._operation_failure(operation.state, str(error))
            with self._condition:
                operation.result = result
                operation.done = True
                if operation.id == (self._pending_join.operation_id if self._pending_join else None):
                    self._pending_join = None
                self._apply_operation_result_locked(operation, result)
                self._active_operation = None
                self._condition.notify_all()

    def _apply_operation_result_locked(self, operation: Operation, result: dict[str, Any]) -> None:
        status = result.get("status")
        if isinstance(status, dict):
            self._status = dict(status)
        else:
            self._status["state"] = STATE_ERROR
            self._status["message"] = result.get("message") or "Wi-Fi operation failed"
            self._status["operation_id"] = None
        self._status["last_operation"] = {
            "id": operation.id,
            "success": result.get("success") is True,
            "message": result.get("message") or "Wi-Fi operation completed",
        }
        self._status["scan_stale"] = self._scan_is_stale_locked()

    def _queue_operation(self, operation: Operation, *, message: str) -> bool:
        with self._condition:
            self._expire_pending_join_locked()
            if self._busy_locked() or self._pending_join is not None:
                return False
            self._queue_operation_locked(operation, message=message)
            return True

    def _queue_operation_locked(self, operation: Operation, *, message: str) -> None:
        self._queued_operation = operation
        self._status["state"] = operation.state
        self._status["operation_id"] = operation.id
        self._status["last_operation"] = None
        self._status["target_ssid"] = self._target_ssid_for_state(operation.state, self._status)
        self._status["message"] = message
        self._status["scan_stale"] = self._scan_is_stale_locked()
        self._condition.notify_all()

    def _busy_locked(self) -> bool:
        return any(
            value is not None
            for value in (self._queued_operation, self._active_operation, self._sync_operation)
        )

    def _operation_in_flight(self) -> bool:
        with self._lock:
            self._expire_pending_join_locked()
            return self._stop or self._busy_locked() or self._pending_join is not None

    def _busy_response(self) -> dict[str, Any]:
        with self._lock:
            return self._busy_response_locked()

    def _busy_response_locked(self) -> dict[str, Any]:
        return self._failure(
            "Wi-Fi controller is busy",
            dict(self._status),
            operation_id=self._status.get("operation_id"),
        )

    def _run_enter_setup(self) -> dict[str, Any]:
        scan_error = None
        if not self._snapshot().get("setup_active"):
            try:
                scan = self.backend.scan_networks()
                with self._lock:
                    self._scan_cache = self._filter_networks(scan)
                    self._scan_at = self.clock.monotonic()
            except BackendError as error:
                scan_error = str(error)
        self._check_stopping()
        self.backend.activate_setup()
        snapshot = self._snapshot()
        self._refresh_metadata()
        return self._success(
            "Setup active",
            self._idle_status_for_snapshot(snapshot, state=STATE_SETUP, message=(
                f"Join {self.config.ap_ssid} to configure Wi-Fi."
                + (f" Scan unavailable: {scan_error}. Enter the SSID manually or refresh." if scan_error else "")
            )),
            networks=list(self._scan_cache),
        )

    def _run_refresh(self) -> dict[str, Any]:
        setup_was_active = bool(self.status().get("setup_active"))
        try:
            if setup_was_active:
                self.backend.deactivate_setup(restore_autoconnect=False)
            scan = self.backend.scan_networks()
            filtered = self._filter_networks(scan if isinstance(scan, list) else [])
            with self._lock:
                self._scan_cache = filtered
                self._scan_at = self.clock.monotonic()
            self._refresh_metadata()
            if setup_was_active:
                self._check_stopping()
                self.backend.activate_setup()
                snapshot = self._snapshot()
                return self._success(
                    "Refresh complete",
                    self._idle_status_for_snapshot(snapshot, state=STATE_SETUP, message="Network list refreshed"),
                    scan=list(filtered),
                )
            snapshot = self._snapshot()
            return self._success(
                "Refresh complete",
                self._idle_status_for_snapshot(snapshot, message="Network list refreshed"),
                scan=list(filtered),
            )
        except BackendError as error:
            self._check_stopping()
            if setup_was_active:
                self.backend.restore_setup()
                snapshot = self._snapshot()
                return self._failure(
                    f"Refresh failed: {error}",
                    self._idle_status_for_snapshot(snapshot, state=STATE_ERROR, message=f"Refresh failed: {error}"),
                )
            snapshot = self._snapshot()
            return self._failure(
                f"Refresh failed: {error}",
                self._idle_status_for_snapshot(snapshot, state=STATE_ERROR, message=f"Refresh failed: {error}"),
            )

    def _run_join(self, pending: PendingJoin) -> dict[str, Any]:
        try:
            self.backend.deactivate_setup(restore_autoconnect=False)
            with self._lock:
                self._status["setup_active"] = False
            self._check_stopping()
            if pending.profile_id and pending.password is None:
                ok, message = self.backend.connect_saved(pending.ssid, profile_id=pending.profile_id)
            else:
                ok, message = self.backend.connect_network(
                    pending.ssid,
                    secret=pending.password,
                    hidden=pending.hidden,
                    profile_id=pending.profile_id,
                    security=pending.security,
                )
            if not ok:
                raise BackendError(message)
            snapshot = self._await_join_success(pending)
            finalize = getattr(self.backend, "finalize_staged_connection", None)
            cleanup_warning: str | None = None
            if callable(finalize):
                result = finalize(pending.profile_id if pending.password is not None else None)
                cleanup_warning = result if isinstance(result, str) and result else None
            self.backend.restore_device_autoconnect()
            self._refresh_metadata()
            success_message = f"Connected to {pending.ssid}"
            if cleanup_warning:
                success_message = f"{success_message} ({cleanup_warning})"
            return self._success(
                success_message,
                self._idle_status_for_snapshot(snapshot, state=STATE_NORMAL, message=success_message),
            )
        except BackendError as error:
            self._check_stopping()
            discard = getattr(self.backend, "discard_staged_connection", None)
            failure_message = f"Join failed: {error}"
            if callable(discard):
                try:
                    discard()
                except BackendError as cleanup_error:
                    failure_message += f"; cleanup: {cleanup_error}"
            with self._lock:
                self._status.update(state=STATE_RECOVERING, message=failure_message)
            try:
                self.backend.restore_setup()
            except BackendError as restore_error:
                raise BackendError(f"{failure_message}; restoring setup: {restore_error}") from restore_error
            snapshot = self._snapshot()
            self._refresh_metadata()
            return self._failure(
                failure_message,
                self._idle_status_for_snapshot(snapshot, state=STATE_ERROR, message=failure_message),
            )

    def _await_join_success(self, pending: PendingJoin, deadline: float | None = None) -> dict[str, Any]:
        if deadline is None:
            deadline = self.clock.monotonic() + self.config.operation_timeout
        while self.clock.monotonic() < deadline:
            self._check_stopping()
            snapshot = self._snapshot()
            if self._snapshot_matches_join(snapshot, pending):
                return snapshot
            self.clock.sleep(0.5)
        raise BackendError("No usable IPv4 address acquired")

    def _check_stopping(self) -> None:
        if self._stop:
            raise BackendError("Wi-Fi controller is stopping")

    def _snapshot_matches_join(self, snapshot: dict[str, Any], pending: PendingJoin) -> bool:
        if snapshot.get("setup_active"):
            return False
        if snapshot.get("wifi_mode") == "ap":
            return False
        if snapshot.get("wifi_ssid") != pending.ssid:
            return False
        if pending.profile_id and pending.password is None and snapshot.get("wifi_profile_id") != pending.profile_id:
            return False
        if pending.password is not None or pending.profile_id is None:
            staged = getattr(self.backend, "staged_profile_id", None)
            if callable(staged) and snapshot.get("wifi_profile_id") != staged():
                return False
        wifi_ipv4 = snapshot.get("wifi_ipv4", snapshot.get("ipv4"))
        if not isinstance(wifi_ipv4, str) or not wifi_ipv4:
            return False
        return True

    def _run_retry_saved(self) -> dict[str, Any]:
        saved_profiles: Any = getattr(self.backend, "saved_profiles", lambda: [])()
        if not isinstance(saved_profiles, list) or not saved_profiles:
            self.backend.restore_setup()
            snapshot = self._snapshot()
            return self._failure(
                "Saved-network retry failed: No saved networks",
                self._idle_status_for_snapshot(snapshot, state=STATE_ERROR, message="Saved-network retry failed: No saved networks"),
            )
        errors: list[str] = []
        deadline = self.clock.monotonic() + self.config.operation_timeout
        try:
            self.backend.deactivate_setup(restore_autoconnect=False)
            with self._lock:
                self._status["setup_active"] = False
            for profile in saved_profiles:
                self._check_stopping()
                if self.clock.monotonic() >= deadline:
                    errors.append("Saved-network retry timed out")
                    break
                ssid = profile.get("ssid")
                profile_id = profile.get("id")
                if not isinstance(ssid, str) or not isinstance(profile_id, str):
                    continue
                ok, message = self.backend.connect_saved(ssid, profile_id=profile_id)
                if not ok:
                    errors.append(f"{ssid}: {message}")
                    continue
                pending = PendingJoin(
                    operation_id=profile_id,
                    ssid=ssid,
                    password=None,
                    hidden=False,
                    security=None,
                    profile_id=profile_id,
                    prepared_at=self.clock.monotonic(),
                )
                try:
                    snapshot = self._await_join_success(pending, deadline=deadline)
                except BackendError:
                    errors.append(f"{ssid}: no usable IPv4")
                    continue
                self.backend.restore_device_autoconnect()
                self._refresh_metadata()
                return self._success(
                    f"Connected to {ssid}",
                    self._idle_status_for_snapshot(snapshot, state=STATE_NORMAL, message=f"Connected to {ssid}"),
                )
            raise BackendError("; ".join(errors) if errors else "No saved networks succeeded")
        except BackendError as error:
            self._check_stopping()
            with self._lock:
                self._status.update(state=STATE_RECOVERING, message=f"Saved-network retry failed: {error}")
            self.backend.restore_setup()
            snapshot = self._snapshot()
            return self._failure(
                f"Saved-network retry failed: {error}",
                self._idle_status_for_snapshot(snapshot, state=STATE_ERROR, message=f"Saved-network retry failed: {error}"),
            )

    def _operation_failure(self, state: str, message: str) -> dict[str, Any]:
        self._retry_not_before = self.clock.monotonic() + self.config.refresh_backoff
        try:
            snapshot = self._snapshot()
        except BackendError:
            snapshot = self._snapshot_defaults()
            snapshot["setup_active"] = False
        return self._failure(
            message,
            self._status_from_snapshot(
                snapshot,
                state=STATE_ERROR,
                message=message,
                setup_active=bool(snapshot.get("setup_active")),
                available=self._backend_available(),
                operation_id=None,
                current=self._status,
            ),
        )

    def _success(self, message: str, status: dict[str, Any], **extra: Any) -> dict[str, Any]:
        response: dict[str, Any] = {"success": True, "message": message, "status": status}
        response.update(extra)
        return response

    def _failure(self, message: str, status: dict[str, Any], **extra: Any) -> dict[str, Any]:
        response: dict[str, Any] = {"success": False, "message": message, "status": status}
        response.update(extra)
        return response

    def _is_connected_snapshot(self, snapshot: dict[str, Any]) -> bool:
        return bool(snapshot.get("ethernet") or snapshot.get("ipv4"))

    def _idle_status_for_snapshot(
        self,
        snapshot: dict[str, Any],
        *,
        state: str | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        current = self.status()
        if state is None:
            state = self._idle_state_for_snapshot(snapshot, current=current)
        if message is None:
            message = self._default_message_for_snapshot(snapshot)
        return self._status_from_snapshot(
            snapshot,
            state=state,
            message=message,
            setup_active=bool(snapshot.get("setup_active")),
            available=self._backend_available(),
            operation_id=None,
            current=current,
        )

    def _apply_idle_snapshot(
        self,
        snapshot: dict[str, Any],
        *,
        state: str | None = None,
        message: str | None = None,
    ) -> None:
        with self._lock:
            self._status = self._status_from_snapshot(
                snapshot,
                state=state or self._idle_state_for_snapshot(snapshot, current=self._status),
                message=message or self._default_message_for_snapshot(snapshot),
                setup_active=bool(snapshot.get("setup_active")),
                available=self._backend_available(),
                operation_id=self._status.get("operation_id") if self._busy_locked() else None,
                current=self._status,
            )

    def _status_from_snapshot(
        self,
        snapshot: dict[str, Any],
        *,
        state: str,
        message: str,
        setup_active: bool,
        available: bool,
        operation_id: str | None,
        current: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = current or {}
        setup_url = snapshot.get("setup_url") or current.get("setup_url") or self._setup_url(self.config.ap_address)
        ap_network = snapshot.get("ap_network") or current.get("ap_network") or self._ap_network(self.config.ap_address)
        return {
            "state": state,
            "setup_active": setup_active,
            "available": available,
            "wifi_ssid": snapshot.get("wifi_ssid"),
            "wifi_signal": snapshot.get("wifi_signal"),
            "ethernet": bool(snapshot.get("ethernet")),
            "ipv4": snapshot.get("ipv4"),
            "hostname": snapshot.get("hostname") or socket.gethostname(),
            "local_url": snapshot.get("local_url") or current.get("local_url") or self._local_url(),
            "setup_url": setup_url,
            "ap_ssid": self.config.ap_ssid,
            "ap_network": ap_network,
            "target_ssid": self._target_ssid_for_state(state, current),
            "message": message,
            "operation_id": operation_id,
            "last_operation": current.get("last_operation"),
            "scan_stale": current.get("scan_stale", True),
            "connectivity": snapshot.get("connectivity") or "unknown",
        }

    def _snapshot_defaults(self) -> dict[str, Any]:
        return {
            "hostname": self._status.get("hostname") or socket.gethostname(),
            "local_url": self._status.get("local_url") or self._local_url(),
            "setup_url": self._status.get("setup_url") or self._setup_url(self.config.ap_address),
            "ap_network": self._status.get("ap_network") or self._ap_network(self.config.ap_address),
            "connectivity": self._status.get("connectivity") or "unknown",
            "wifi_ssid": self._status.get("wifi_ssid"),
            "wifi_signal": self._status.get("wifi_signal"),
            "ethernet": self._status.get("ethernet"),
            "ipv4": self._status.get("ipv4"),
        }

    def _idle_state_for_snapshot(self, snapshot: dict[str, Any], *, current: dict[str, Any]) -> str:
        if snapshot.get("setup_active"):
            if current.get("state") in {STATE_RECOVERING, STATE_ERROR, STATE_JOIN_PENDING}:
                return str(current["state"])
            return STATE_SETUP
        if self._is_connected_snapshot(snapshot):
            return STATE_NORMAL
        if current.get("state") == STATE_STARTING and self.clock.monotonic() - self._started_at < self.config.boot_grace:
            return STATE_STARTING
        if current.get("state") in {STATE_RECOVERING, STATE_ERROR}:
            return str(current["state"])
        return STATE_NORMAL

    def _default_message_for_snapshot(self, snapshot: dict[str, Any]) -> str:
        if snapshot.get("setup_active"):
            return f"Join {self.config.ap_ssid} to configure Wi-Fi."
        if snapshot.get("wifi_ssid"):
            return f"Connected to {snapshot['wifi_ssid']}"
        if snapshot.get("ethernet"):
            return "Ethernet connected"
        if self.clock.monotonic() - self._started_at < self.config.boot_grace:
            return "Waiting for saved networks."
        return "Wi-Fi is not connected."

    def _scan_is_stale_locked(self) -> bool:
        if not self._scan_at:
            return True
        return (self.clock.monotonic() - self._scan_at) > self.config.scan_freshness

    def _pending_join_matches_locked(self, prepared: dict[str, Any]) -> bool:
        pending = self._pending_join
        return pending is not None and prepared == {
            "ssid": pending.ssid,
            "password": pending.password,
            "hidden": pending.hidden,
            "security": pending.security,
            "profile_id": pending.profile_id,
        }

    def _target_ssid_for_state(
        self,
        state: str,
        current: dict[str, Any] | None = None,
    ) -> str | None:
        if state not in {STATE_JOIN_PENDING, STATE_JOINING}:
            return None
        if self._pending_join is not None:
            return self._pending_join.ssid
        if current is not None:
            value = current.get("target_ssid")
            if isinstance(value, str) and value:
                return value
        return None

    def _expire_pending_join_locked(self) -> None:
        pending = self._pending_join
        if pending is None or pending.committed:
            return
        if self.clock.monotonic() - pending.prepared_at <= self.config.operation_timeout:
            return
        self._pending_join = None
        setup_active = bool(self._status.get("setup_active"))
        self._status = self._status_from_snapshot(
            self._snapshot_defaults(),
            state=STATE_SETUP if setup_active else STATE_NORMAL,
            message="Prepared Wi-Fi handoff expired. Start again.",
            setup_active=setup_active,
            available=self._backend_available(),
            operation_id=None,
            current=self._status,
        )

    def _filter_networks(self, networks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            network
            for network in networks
            if isinstance(network, dict)
            and network.get("ssid") not in {self.config.ap_ssid, self.config.ap_connection_id}
        ]

    def _normalize_ssid(self, value: Any) -> str:
        normalized = wifi_manager._normalize_ssid(value)
        if normalized is None:
            raise ValueError("Invalid SSID")
        return normalized

    def _normalize_optional_password(self, params: dict[str, Any], *, required: bool) -> str | None:
        if "password" not in params or params.get("password") is None:
            if required:
                raise ValueError("password is required")
            return None
        value = params.get("password")
        if not isinstance(value, str) or any(char in value for char in ("\x00", "\n", "\r")) or len(value) > 128:
            raise ValueError("password must be a string")
        if required and not value:
            raise ValueError("password is required")
        return value or None

    def _normalize_profile_id(self, value: Any) -> str | None:
        if value in (None, ""):
            return None
        if not isinstance(value, str) or not wifi_manager._UUID_RE.fullmatch(value):
            raise ValueError("profile_id must be a UUID")
        return value

    def _saved_profile_id(self, ssid: str, profile_id: str | None) -> str:
        with self._lock:
            matches = [profile for profile in self._profiles_cache
                       if profile.get("ssid") == ssid
                       and (profile_id is None or profile.get("id") == profile_id)]
        if len(matches) != 1:
            raise ValueError("Select an existing saved Wi-Fi profile from the network list")
        return str(matches[0]["id"])

    def _managed_ap_profile_id(self) -> str | None:
        getter = getattr(self.backend, "managed_ap_profile_id", None)
        if callable(getter):
            value = getter()
            return value if isinstance(value, str) and value else None
        return None

    def _normalize_security(self, value: Any) -> str | None:
        if value in (None, ""):
            return None
        if not isinstance(value, str) or len(value) > 32 or any(char in value for char in ("\x00", "\n", "\r")):
            raise ValueError("security must be a string")
        lowered = value.strip().lower()
        if not lowered:
            return None
        if lowered not in {"auto", "open", "wpa2", "wpa3"}:
            raise ValueError("Unsupported Wi-Fi security type")
        return lowered

    def _security_needs_password(self, security: str | None) -> bool:
        if security is None:
            return False
        return any(token in security for token in ("wpa", "wep", "sae", "psk"))

    def _normalize_bool(self, value: Any) -> bool:
        if value is None:
            return False
        if not isinstance(value, bool):
            raise ValueError("Expected a boolean")
        return value

    def _next_operation_id(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4()}"

    def _setup_url(self, address: str) -> str:
        interface = ipaddress.ip_interface(address)
        return f"http://{interface.ip}:{self.config.ap_port}/wifi/setup"

    def _ap_network(self, address: str) -> str:
        return str(ipaddress.ip_interface(address).network)

    def _local_url(self) -> str:
        return f"http://{socket.gethostname()}.local:{self.config.ap_port}/"


class WifiProvisioningServer:
    def __init__(self, controller: ProvisioningController):
        self.controller = controller
        self._server: socket.socket | None = None
        self._stop = threading.Event()
        self._clients = threading.BoundedSemaphore(16)

    def serve_forever(self) -> None:
        config = self.controller.config
        path = config.socket_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server = server
        try:
            server.bind(path)
            os.chmod(path, 0o660)
            if config.socket_group:
                gid = grp.getgrnam(config.socket_group).gr_gid
                os.chown(path, 0, gid)
            server.listen(16)
            server.settimeout(1.0)
            while not self._stop.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    raise
                if not self._clients.acquire(blocking=False):
                    conn.close()
                    continue
                thread = threading.Thread(
                    target=self._serve_connection,
                    args=(conn,),
                    daemon=True,
                )
                thread.start()
        finally:
            server.close()
            self._server = None
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def close(self) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.close()

    def _serve_connection(self, conn: socket.socket) -> None:
        try:
            with conn:
                try:
                    self._handle_connection(conn)
                except OSError:
                    return
        finally:
            self._clients.release()

    def _handle_connection(self, conn: socket.socket) -> None:
        conn.settimeout(2.0)
        deadline = time.monotonic() + 2.0
        data = b""
        while len(data) <= MAX_REQUEST_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            conn.settimeout(remaining)
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break
        if len(data) > MAX_REQUEST_BYTES:
            self._send(conn, {"success": False, "message": "Request too large"})
            return
        payload = data.split(b"\n", 1)[0].strip()
        if not payload:
            self._send(conn, {"success": False, "message": "Empty request"})
            return
        try:
            request = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(conn, {"success": False, "message": "Invalid JSON"})
            return
        if not isinstance(request, dict):
            self._send(conn, {"success": False, "message": "Invalid request"})
            return
        response = self.controller.request(request)
        self._send(conn, response)

    def _send(self, conn: socket.socket, response: dict[str, Any]) -> None:
        wire = json.dumps(response, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
        if len(wire) > MAX_RESPONSE_BYTES:
            wire = json.dumps({"success": False, "message": "Response too large"}).encode("utf-8") + b"\n"
        conn.sendall(wire)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CTS scoreboard Wi-Fi provisioning controller")
    parser.add_argument("--interface", default=DEFAULT_INTERFACE)
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--socket-group")
    parser.add_argument("--ap-address", default=DEFAULT_AP_ADDRESS)
    parser.add_argument("--ap-ssid", default=AP_SSID)
    parser.add_argument("--ap-connection-id", default=AP_CONNECTION_ID)
    parser.add_argument("--ap-port", type=int, default=DEFAULT_AP_PORT)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    config = ProvisioningConfig(
        interface=args.interface,
        socket_path=args.socket,
        socket_group=args.socket_group,
        ap_address=args.ap_address,
        ap_ssid=args.ap_ssid,
        ap_connection_id=args.ap_connection_id,
        ap_port=args.ap_port,
    )
    backend = SystemProvisioningBackend(
        config.interface, config.ap_connection_id, ap_ssid=config.ap_ssid,
        ap_address=config.ap_address, ap_port=config.ap_port,
        runtime_dir=os.path.dirname(config.socket_path), runtime_group=config.socket_group,
    )
    backend._ensure_runtime_dir()
    with open(os.path.join(backend.runtime_dir, "controller.lock"), "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(1, "Wi-Fi provisioning controller is already running\n")
        return _serve(config, backend)


def _serve(config: ProvisioningConfig, backend: SystemProvisioningBackend) -> int:
    server: WifiProvisioningServer | None = None
    stopping = False

    def stop_handler(signum: int, frame: Any) -> None:  # noqa: ARG001
        nonlocal stopping
        stopping = True
        if server is not None:
            server.close()

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    controller = ProvisioningController(config, backend=backend)
    server = WifiProvisioningServer(controller)
    try:
        if not stopping:
            server.serve_forever()
    finally:
        server.close()
        controller.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
