"""Local Unix-socket client for the Wi-Fi provisioning controller."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from typing import Any

DEFAULT_SOCKET = "/run/cts-scoreboard-wifi/control.sock"
MAX_MESSAGE_BYTES = 64 * 1024
DEFAULT_TIMEOUT = 3.0


class ProvisioningError(RuntimeError):
    pass


def is_enabled() -> bool:
    value = os.getenv("CTS_WIFI_PROVISIONING", "")
    return value.lower() in {"1", "true", "yes", "on"}


def _socket_path() -> str:
    return os.getenv("CTS_WIFI_SOCKET", DEFAULT_SOCKET)


def _validate_response(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise ProvisioningError("Invalid provisioning response")
    success = response.get("success")
    message = response.get("message")
    if not isinstance(success, bool) or not isinstance(message, str):
        raise ProvisioningError("Invalid provisioning response")
    status = response.get("status")
    if status is not None and not isinstance(status, dict):
        raise ProvisioningError("Invalid provisioning response")
    operation_id = response.get("operation_id")
    if operation_id is not None and not isinstance(operation_id, str):
        raise ProvisioningError("Invalid provisioning response")
    for key in ("networks", "saved", "profiles"):
        value = response.get(key)
        if value is not None and not isinstance(value, list):
            raise ProvisioningError("Invalid provisioning response")
    return response


def _request_payload(payload: dict[str, Any], *, require_enabled: bool) -> dict[str, Any]:
    if require_enabled and not is_enabled():
        raise ProvisioningError("Wi-Fi provisioning is not enabled")
    try:
        wire = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as error:
        raise ProvisioningError("Invalid provisioning request") from error
    if len(wire) > MAX_MESSAGE_BYTES:
        raise ProvisioningError("Provisioning request too large")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        deadline = time.monotonic() + DEFAULT_TIMEOUT
        sock.settimeout(DEFAULT_TIMEOUT)
        sock.connect(_socket_path())
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProvisioningError("Provisioning request timed out")
        sock.settimeout(remaining)
        sock.sendall(wire)
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_MESSAGE_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProvisioningError("Provisioning response timed out")
            sock.settimeout(remaining)
            data = sock.recv(4096)
            if not data:
                break
            total += len(data)
            if total > MAX_MESSAGE_BYTES:
                raise ProvisioningError("Provisioning response too large")
            chunks.append(data)
            if b"\n" in data:
                break
        raw = b"".join(chunks).split(b"\n", 1)[0].strip()
        if not raw:
            raise ProvisioningError("Empty provisioning response")
        try:
            response = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ProvisioningError("Invalid provisioning response") from error
        return _validate_response(response)
    except (OSError, socket.timeout) as error:
        raise ProvisioningError(str(error) or "Provisioning transport failure") from error
    finally:
        sock.close()


def request(command: str, **params: Any) -> dict[str, Any]:
    if not isinstance(command, str) or not command:
        raise ProvisioningError("Invalid provisioning command")
    if "ap_network" in params:
        raise ProvisioningError("ap_network is controller-owned")
    payload = {"command": command, "params": params}
    return _request_payload(payload, require_enabled=True)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CTS scoreboard Wi-Fi provisioning client")
    parser.add_argument("command")
    parser.add_argument(
        "params_json",
        nargs="?",
        default="{}",
        help="Optional JSON object of request params",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        params = json.loads(args.params_json)
    except json.JSONDecodeError:
        print(json.dumps({"success": False, "message": "Invalid params JSON"}), file=sys.stderr)
        return 2
    if not isinstance(params, dict):
        print(json.dumps({"success": False, "message": "params_json must decode to an object"}), file=sys.stderr)
        return 2
    try:
        response = _request_payload(
            {"command": args.command, "params": params},
            require_enabled=False,
        )
    except ProvisioningError as error:
        print(json.dumps({"success": False, "message": str(error)}), file=sys.stderr)
        return 1
    print(json.dumps(response, sort_keys=True))
    return 0 if response.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
