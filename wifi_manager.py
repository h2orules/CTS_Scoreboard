"""Wi-Fi network management via NetworkManager (nmcli)."""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable

_SAFE_TEXT_RE = re.compile(r"[^\x00-\x1f\x7f]+\Z")
_SAFE_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9_.:@+=,][A-Za-z0-9_.:/@+=,\- ]{0,127}\Z")
_SAFE_INTERFACE_RE = re.compile(r"[A-Za-z0-9_.:-]{1,32}\Z")
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)
_MAX_TEXT_LEN = 128
_MAX_SSID_BYTES = 32
_MAX_SECRET_LEN = 128


@dataclass(frozen=True)
class WifiNetwork:
    ssid: str
    signal: int
    security: str
    in_use: bool


@dataclass(frozen=True)
class WifiProfile:
    id: str
    ssid: str
    type: str = ""
    autoconnect: bool = False
    device: str | None = None
    active: bool = False
    name: str | None = None
    mode: str | None = None


def _is_safe_nmcli_value(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > _MAX_TEXT_LEN:
        return False
    if value[0] == "-":
        return False
    return _SAFE_TEXT_RE.fullmatch(value) is not None


def _is_safe_nmcli_arg_list(args: Any) -> bool:
    if not isinstance(args, list):
        return False
    for arg in args:
        if not isinstance(arg, str) or not arg:
            return False
        if _SAFE_TEXT_RE.fullmatch(arg) is None:
            return False
    return True


def _sanitize_token(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("nmcli argument must be a string")
    if not value or len(value) > _MAX_TEXT_LEN:
        raise ValueError("Unsafe nmcli argument")
    if value[0] == "-":
        raise ValueError("Unsafe nmcli argument")
    if _SAFE_IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError("Unsafe nmcli argument")
    return value


def _normalize_ssid(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if _SAFE_TEXT_RE.fullmatch(value) is None:
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(encoded) > _MAX_SSID_BYTES:
        return None
    return value


def _normalize_interface(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if _SAFE_INTERFACE_RE.fullmatch(value) is None:
        return None
    return value


def _normalize_secret(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > _MAX_SECRET_LEN:
        return None
    if _SAFE_TEXT_RE.fullmatch(value) is None:
        return None
    return value


def _nmcli_prefix() -> list[str]:
    return ["nmcli"] if getattr(os, "geteuid", lambda: 1)() == 0 else ["sudo", "nmcli"]


def is_available() -> bool:
    return shutil.which("nmcli") is not None


def _run(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    if not _is_safe_nmcli_arg_list(args):
        return -1, "", "Invalid nmcli arguments"
    try:
        result = subprocess.run(
            _nmcli_prefix() + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except FileNotFoundError:
        return -1, "", "nmcli not found"
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"
    return result.returncode, result.stdout, result.stderr


Runner = Callable[[list[str], int], tuple[int, str, str]]


def _call_runner(
    runner: Callable[..., tuple[int, str, str]] | None,
    args: list[str],
    timeout: int = 30,
) -> tuple[int, str, str]:
    if runner is None:
        runner = _run
    return runner(args, timeout=timeout)


def _split_terse(line: str) -> list[str]:
    fields: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(line):
        char = line[index]
        if char == "\\" and index + 1 < len(line) and line[index + 1] in {":", "\\"}:
            current.append(line[index + 1])
            index += 2
            continue
        if char == ":":
            fields.append("".join(current))
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    fields.append("".join(current))
    return fields


def _split_get_values(output: str, expected: int) -> list[str]:
    values = output.splitlines()
    if len(values) < expected:
        return []
    return values[:expected]


def _strip_nmcli_message(text: str) -> str:
    cleaned = " ".join((text or "").split())
    return cleaned[:240]


def _connection_selector(connection_id: str) -> list[str]:
    return ["uuid", connection_id] if _UUID_RE.fullmatch(connection_id) else ["id", connection_id]


def _bool_from_text(value: str) -> bool:
    return value.strip().lower() in {"yes", "true", "1", "activated", "enabled"}


def _resolve_saved_connection_target(
    target: str,
    runner: Callable[..., tuple[int, str, str]] | None = None,
) -> tuple[str, str, str] | None:
    if _UUID_RE.fullmatch(target):
        info = show_connection(target, runner=runner)
        display = info.get("ssid") if isinstance(info, dict) else None
        return "uuid", target, display if isinstance(display, str) and display else target
    normalized_ssid = _normalize_ssid(target)
    if normalized_ssid is None:
        return None
    matches = [
        profile
        for profile in list_connection_profiles(runner=runner)
        if profile.get("ssid") == normalized_ssid
    ]
    if matches:
        matches.sort(
            key=lambda profile: (
                not bool(profile.get("active")),
                not bool(profile.get("autoconnect")),
                str(profile.get("id") or ""),
            )
        )
        profile_id = matches[0].get("id")
        if isinstance(profile_id, str) and profile_id:
            return "uuid", profile_id, normalized_ssid
    return "id", normalized_ssid, normalized_ssid


def _parse_signal(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0


def _parse_ip4_values(output: str) -> list[str]:
    addresses: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        _, _, value = line.partition(":")
        value = value.strip() or line.strip()
        if not value:
            continue
        address = value.split("/", 1)[0].strip()
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if isinstance(ip, ipaddress.IPv4Address):
            addresses.append(str(ip))
    return addresses


def get_device_ipv4_addresses(device: str, runner: Callable[..., tuple[int, str, str]] | None = None) -> list[str]:
    device_name = _normalize_interface(device)
    if device_name is None:
        return []
    code, out, _ = _call_runner(runner, ["-t", "-f", "IP4.ADDRESS", "dev", "show", device_name])
    if code != 0:
        return []
    return _parse_ip4_values(out)


def device_has_usable_ipv4(
    device: str,
    runner: Callable[..., tuple[int, str, str]] | None = None,
    excluded_networks: list[ipaddress.IPv4Network] | None = None,
) -> bool:
    for address_text in get_device_ipv4_addresses(device, runner=runner):
        address = ipaddress.ip_address(address_text)
        if not isinstance(address, ipaddress.IPv4Address):
            continue
        if address.is_loopback or address.is_link_local or address.is_unspecified:
            continue
        if excluded_networks and any(address in network for network in excluded_networks):
            continue
        return True
    return False


def list_active_connections(runner: Callable[..., tuple[int, str, str]] | None = None) -> list[dict[str, str]]:
    code, out, _ = _call_runner(runner, ["-t", "-f", "UUID,NAME,TYPE,DEVICE", "con", "show", "--active"])
    if code != 0:
        return []
    active: list[dict[str, str]] = []
    for line in out.splitlines():
        parts = _split_terse(line)
        if len(parts) < 4 or not parts[0]:
            continue
        active.append({"id": parts[0], "name": parts[1], "type": parts[2], "device": parts[3]})
    return active


def list_device_statuses(runner: Callable[..., tuple[int, str, str]] | None = None) -> list[dict[str, str]]:
    code, out, _ = _call_runner(runner, ["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "dev", "status"])
    if code != 0:
        return []
    devices: list[dict[str, str]] = []
    for line in out.splitlines():
        parts = _split_terse(line)
        if len(parts) < 4 or not parts[0]:
            continue
        devices.append(
            {
                "device": parts[0],
                "type": parts[1],
                "state": parts[2],
                "connection": parts[3],
            }
        )
    return devices


def scan_networks(
    runner: Callable[..., tuple[int, str, str]] | None = None,
    sleeper: Callable[[float], None] | None = None,
    interface: str | None = None,
) -> list[dict[str, Any]]:
    if sleeper is None:
        sleeper = time.sleep
    args = ["dev", "wifi", "rescan"]
    interface_name: str | None = None
    if interface:
        interface_name = _normalize_interface(interface)
        if interface_name is None:
            return []
        args += ["ifname", interface_name]
    code, _, _ = _call_runner(runner, args, timeout=10)
    if code != 0:
        return []
    sleeper(0)
    list_args = ["-t", "-f", "SSID,SIGNAL,SECURITY,IN-USE", "dev", "wifi", "list"]
    if interface_name:
        list_args += ["ifname", interface_name]
    list_args += ["--rescan", "no"]
    code, out, _ = _call_runner(runner, list_args)
    if code != 0:
        return []
    best_by_ssid: dict[str, dict[str, Any]] = {}
    for line in out.splitlines():
        parts = _split_terse(line)
        if len(parts) < 4:
            continue
        ssid = parts[0]
        if not ssid:
            continue
        signal = _parse_signal(parts[1])
        record = {
            "ssid": ssid,
            "signal": signal,
            "security": "" if parts[2] == "--" else parts[2],
            "in_use": parts[3].strip() == "*",
        }
        current = best_by_ssid.get(ssid)
        if current is None or signal > int(current.get("signal", 0)):
            best_by_ssid[ssid] = record
    return sorted(best_by_ssid.values(), key=lambda item: int(item["signal"]), reverse=True)


def _active_wifi_signal(runner: Callable[..., tuple[int, str, str]] | None = None) -> int | None:
    code, out, _ = _call_runner(runner, ["-t", "-f", "IN-USE,SIGNAL", "dev", "wifi", "list", "--rescan", "no"])
    if code != 0:
        return None
    for line in out.splitlines():
        parts = _split_terse(line)
        if len(parts) >= 2 and parts[0].strip() == "*":
            return _parse_signal(parts[1])
    return None


def show_connection(connection_id: str, runner: Callable[..., tuple[int, str, str]] | None = None) -> dict[str, Any]:
    if not _is_safe_nmcli_value(connection_id):
        return {}
    selector = _connection_selector(connection_id)
    code, out, _ = _call_runner(
        runner,
        [
            "--escape", "no",
            "-g",
            (
                "connection.id,connection.uuid,connection.type,connection.autoconnect,"
                "connection.interface-name,802-11-wireless.ssid,802-11-wireless.mode"
            ),
            "con",
            "show",
            selector[0],
            connection_id,
        ],
    )
    if code != 0:
        return {}
    parts = _split_get_values(out, 7)
    if len(parts) < 7:
        return {}
    return {
        "id": parts[1] or connection_id,
        "name": parts[0] or connection_id,
        "type": parts[2],
        "autoconnect": _bool_from_text(parts[3]),
        "device": parts[4] or None,
        "ssid": parts[5] or parts[0] or connection_id,
        "mode": parts[6] or None,
    }


def list_connection_profiles(runner: Callable[..., tuple[int, str, str]] | None = None) -> list[dict[str, Any]]:
    code, out, _ = _call_runner(runner, ["-t", "-f", "UUID,TYPE,NAME,AUTOCONNECT,DEVICE", "con", "show"])
    if code != 0:
        return []
    active_ids = {item["id"] for item in list_active_connections(runner=runner)}
    profiles: list[dict[str, Any]] = []
    for line in out.splitlines():
        parts = _split_terse(line)
        if len(parts) < 5 or not parts[0] or parts[1] != "802-11-wireless":
            continue
        uuid = parts[0]
        name = parts[2]
        info = show_connection(uuid, runner=runner)
        if not info:
            info = {
                "id": uuid,
                "ssid": name,
                "name": name,
                "type": parts[1],
                "autoconnect": _bool_from_text(parts[3]),
                "device": parts[4] or None,
                "mode": None,
            }
        profiles.append(
            {
                "id": info["id"],
                "ssid": info.get("ssid") or name,
                "name": info.get("name") or name,
                "type": info.get("type") or parts[1],
                "autoconnect": bool(info.get("autoconnect")),
                "device": info.get("device") or (parts[4] or None),
                "active": info.get("id") in active_ids,
                "mode": info.get("mode"),
            }
        )
    return profiles


def get_saved_networks(runner: Callable[..., tuple[int, str, str]] | None = None) -> list[str]:
    seen: set[str] = set()
    saved: list[str] = []
    for profile in list_connection_profiles(runner=runner):
        ssid = profile.get("ssid")
        if isinstance(ssid, str) and ssid and ssid not in seen:
            seen.add(ssid)
            saved.append(ssid)
    return saved


def get_status(runner: Callable[..., tuple[int, str, str]] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"wifi_ssid": None, "wifi_signal": None, "ethernet": False}
    devices = list_device_statuses(runner=runner)
    active_connections_by_device = {
        item["device"]: item for item in list_active_connections(runner=runner) if item.get("device")
    }
    for device in devices:
        if device["type"] == "ethernet" and device["state"] == "connected":
            if device_has_usable_ipv4(device["device"], runner=runner):
                result["ethernet"] = True
        if device["type"] != "wifi" or device["state"] != "connected":
            continue
        active = active_connections_by_device.get(device["device"])
        lookup = active["id"] if active and active.get("id") else device["connection"]
        info = show_connection(lookup, runner=runner) if lookup else {}
        result["wifi_ssid"] = info.get("ssid") or device["connection"] or None
        result["wifi_signal"] = _active_wifi_signal(runner=runner)
    return result


def set_autoconnect(
    connection_id: str,
    enabled: bool,
    runner: Callable[..., tuple[int, str, str]] | None = None,
) -> tuple[bool, str]:
    if not _is_safe_nmcli_value(connection_id):
        return False, "Invalid connection id"
    selector = _connection_selector(connection_id)
    code, out, err = _call_runner(
        runner,
        ["con", "modify", selector[0], connection_id, "connection.autoconnect", "yes" if enabled else "no"],
    )
    if code == 0:
        return True, f"Updated autoconnect for {connection_id}"
    return False, _strip_nmcli_message(err or out or "Failed to update connection")


def connect(
    ssid: str,
    secret: str | None = None,
    hidden: bool = False,
    interface: str | None = None,
    saved: bool = True,
    runner: Callable[..., tuple[int, str, str]] | None = None,
    **kwargs: Any,
) -> tuple[bool, str]:
    if secret is None and "password" in kwargs:
        secret = kwargs["password"]
    if interface is not None:
        interface_name = _normalize_interface(interface)
        if interface_name is None:
            return False, "Invalid interface"
    else:
        interface_name = None

    if saved and secret in (None, ""):
        saved_target = _resolve_saved_connection_target(ssid, runner=runner)
        if saved_target is not None:
            selector, connection_target, display_name = saved_target
            args = ["con", "up", selector, connection_target]
            if interface_name:
                args += ["ifname", interface_name]
            code, out, err = _call_runner(runner, args)
            if code == 0:
                return True, f"Connected to {display_name}"
            return False, _strip_nmcli_message(err or out or "Failed to connect")

    normalized_ssid = _normalize_ssid(ssid)
    if normalized_ssid is None:
        return False, "Invalid SSID"
    if secret not in (None, ""):
        normalized_secret = _normalize_secret(secret)
        if normalized_secret is None:
            return False, "Invalid password"
        args = ["dev", "wifi", "connect", normalized_ssid]
        if interface_name:
            args += ["ifname", interface_name]
        if hidden:
            args += ["hidden", "yes"]
        args += ["password", normalized_secret]
    elif hidden or not saved:
        args = ["dev", "wifi", "connect", normalized_ssid]
        if interface_name:
            args += ["ifname", interface_name]
        if hidden:
            args += ["hidden", "yes"]
    else:
        args = ["con", "up", "id", normalized_ssid]
        if interface_name:
            args += ["ifname", interface_name]

    code, out, err = _call_runner(runner, args)
    if code == 0:
        return True, f"Connected to {normalized_ssid}"
    return False, _strip_nmcli_message(err or out or "Failed to connect")


def forget(connection_id: str, runner: Callable[..., tuple[int, str, str]] | None = None) -> tuple[bool, str]:
    if not _is_safe_nmcli_value(connection_id):
        return False, "Invalid SSID"
    selector = _connection_selector(connection_id)
    code, out, err = _call_runner(runner, ["con", "delete", selector[0], connection_id])
    if code == 0:
        return True, f"Forgot {connection_id}"
    return False, _strip_nmcli_message(err or out or "Failed to forget network")


def update_password(
    connection_id: str,
    secret: str,
    runner: Callable[..., tuple[int, str, str]] | None = None,
) -> tuple[bool, str]:
    if not _is_safe_nmcli_value(connection_id):
        return False, "Invalid SSID"
    normalized_secret = _normalize_secret(secret)
    if normalized_secret is None:
        return False, "Invalid password"
    selector = _connection_selector(connection_id)
    code, out, _ = _call_runner(
        runner, ["-g", "802-11-wireless-security.key-mgmt", "con", "show", selector[0], connection_id],
    )
    if code != 0:
        return False, "Cannot read saved network security"
    key_mgmt = out.strip() or "wpa-psk"
    if key_mgmt not in {"wpa-psk", "sae"}:
        return False, "Only personal WPA networks support password updates"
    code, out, err = _call_runner(
        runner,
        [
            "con",
            "modify",
            selector[0],
            connection_id,
            "wifi-sec.key-mgmt",
            key_mgmt,
            "wifi-sec.psk",
            normalized_secret,
        ],
    )
    if code == 0:
        return True, f"Password updated for {connection_id}"
    return False, "Failed to update password; check the network security and password"


def activate_connection(
    connection_id: str,
    interface: str | None = None,
    runner: Callable[..., tuple[int, str, str]] | None = None,
) -> tuple[bool, str]:
    if not _is_safe_nmcli_value(connection_id):
        return False, "Invalid connection id"
    if interface is not None:
        interface_name = _normalize_interface(interface)
        if interface_name is None:
            return False, "Invalid interface"
    else:
        interface_name = None
    selector = _connection_selector(connection_id)
    args = ["con", "up", selector[0], connection_id]
    if interface_name:
        args += ["ifname", interface_name]
    code, out, err = _call_runner(runner, args)
    if code == 0:
        return True, f"Activated {connection_id}"
    return False, _strip_nmcli_message(err or out or "Failed to activate connection")


def deactivate_connection(
    connection_id: str,
    runner: Callable[..., tuple[int, str, str]] | None = None,
) -> tuple[bool, str]:
    if not _is_safe_nmcli_value(connection_id):
        return False, "Invalid connection id"
    selector = _connection_selector(connection_id)
    code, out, err = _call_runner(runner, ["con", "down", selector[0], connection_id])
    if code == 0:
        return True, f"Deactivated {connection_id}"
    return False, _strip_nmcli_message(err or out or "Failed to deactivate connection")


def add_wifi_profile(
    connection_id: str,
    ssid: str,
    interface: str,
    *,
    secret: str | None = None,
    hidden: bool = False,
    autoconnect: bool = True,
    security: str | None = None,
    managed_stage: bool = False,
    runner: Callable[..., tuple[int, str, str]] | None = None,
) -> tuple[bool, str]:
    normalized_id = _sanitize_token(connection_id)
    normalized_ssid = _normalize_ssid(ssid)
    interface_name = _normalize_interface(interface)
    if normalized_ssid is None or interface_name is None:
        return False, "Invalid Wi-Fi profile"
    if security not in (None, "auto", "open", "wpa2", "wpa3"):
        return False, "Unsupported Wi-Fi security"
    if security == "open" and secret:
        return False, "Open networks do not use a password"
    normalized_secret = _normalize_secret(secret) if secret else None
    if secret and normalized_secret is None:
        return False, "Invalid password"
    args = [
        "con",
        "add",
        "type",
        "wifi",
        "ifname",
        interface_name,
        "con-name",
        normalized_id,
        "ssid",
        normalized_ssid,
        "connection.autoconnect",
        "yes" if autoconnect else "no",
        "802-11-wireless.hidden",
        "yes" if hidden else "no",
    ]
    if normalized_secret:
        args += ["wifi-sec.key-mgmt", "sae" if security == "wpa3" else "wpa-psk",
                 "wifi-sec.psk", normalized_secret]
    if managed_stage:
        args += ["user.data", "org.cts-scoreboard.role=staging"]
    code, out, err = _call_runner(runner, args)
    if code == 0:
        return True, normalized_id
    return False, "Failed to create Wi-Fi profile; check the network security and password"


def set_device_autoconnect(
    interface: str,
    enabled: bool,
    runner: Callable[..., tuple[int, str, str]] | None = None,
) -> tuple[bool, str]:
    interface_name = _normalize_interface(interface)
    if interface_name is None:
        return False, "Invalid interface"
    code, out, err = _call_runner(runner, ["device", "set", interface_name, "autoconnect", "yes" if enabled else "no"])
    if code == 0:
        return True, f"Updated device autoconnect for {interface_name}"
    return False, _strip_nmcli_message(err or out or "Failed to update device autoconnect")


def get_device_autoconnect(
    interface: str,
    runner: Callable[..., tuple[int, str, str]] | None = None,
) -> bool | None:
    interface_name = _normalize_interface(interface)
    if interface_name is None:
        return None
    code, out, _ = _call_runner(runner, ["-t", "-f", "GENERAL.AUTOCONNECT", "dev", "show", interface_name])
    if code != 0:
        return None
    for line in out.splitlines():
        _, _, value = line.partition(":")
        value = value.strip() or line.strip()
        if not value:
            continue
        return _bool_from_text(value)
    return None


class NetworkManagerAdapter:
    def __init__(
        self,
        runner: Callable[..., tuple[int, str, str]] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ):
        self._runner = runner or _run
        self._sleeper = sleeper or time.sleep

    def scan_networks(self, interface: str | None = None) -> list[dict[str, Any]]:
        return scan_networks(self._runner, self._sleeper, interface=interface)

    def get_status(self) -> dict[str, Any]:
        return get_status(self._runner)

    def get_saved_networks(self) -> list[str]:
        return get_saved_networks(self._runner)

    def list_connection_profiles(self) -> list[dict[str, Any]]:
        return list_connection_profiles(self._runner)

    def list_active_connections(self) -> list[dict[str, str]]:
        return list_active_connections(self._runner)

    def list_device_statuses(self) -> list[dict[str, str]]:
        return list_device_statuses(self._runner)

    def get_device_ipv4_addresses(self, device: str) -> list[str]:
        return get_device_ipv4_addresses(device, self._runner)

    def device_has_usable_ipv4(
        self,
        device: str,
        excluded_networks: list[ipaddress.IPv4Network] | None = None,
    ) -> bool:
        return device_has_usable_ipv4(device, self._runner, excluded_networks)

    def connect(
        self,
        ssid: str,
        secret: str | None = None,
        hidden: bool = False,
        interface: str | None = None,
        **kwargs: Any,
    ) -> tuple[bool, str]:
        return connect(
            ssid,
            secret=secret,
            hidden=hidden,
            interface=interface,
            runner=self._runner,
            **kwargs,
        )

    def forget(self, connection_id: str) -> tuple[bool, str]:
        return forget(connection_id, runner=self._runner)

    def update_password(self, connection_id: str, secret: str) -> tuple[bool, str]:
        return update_password(connection_id, secret, runner=self._runner)

    def set_autoconnect(self, connection_id: str, enabled: bool) -> tuple[bool, str]:
        return set_autoconnect(connection_id, enabled, runner=self._runner)

    def activate_connection(self, connection_id: str, interface: str | None = None) -> tuple[bool, str]:
        return activate_connection(connection_id, interface=interface, runner=self._runner)

    def deactivate_connection(self, connection_id: str) -> tuple[bool, str]:
        return deactivate_connection(connection_id, runner=self._runner)

    def show_connection(self, connection_id: str) -> dict[str, Any]:
        return show_connection(connection_id, runner=self._runner)

    def add_wifi_profile(
        self,
        connection_id: str,
        ssid: str,
        interface: str,
        *,
        secret: str | None = None,
        hidden: bool = False,
        autoconnect: bool = True,
    ) -> tuple[bool, str]:
        return add_wifi_profile(
            connection_id,
            ssid,
            interface,
            secret=secret,
            hidden=hidden,
            autoconnect=autoconnect,
            runner=self._runner,
        )

    def set_device_autoconnect(self, interface: str, enabled: bool) -> tuple[bool, str]:
        return set_device_autoconnect(interface, enabled, runner=self._runner)

    def get_device_autoconnect(self, interface: str) -> bool | None:
        return get_device_autoconnect(interface, runner=self._runner)
