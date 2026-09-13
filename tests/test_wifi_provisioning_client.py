import json
import socket
import threading
from pathlib import Path

import pytest

import wifi_provisioning_client as client


def test_is_enabled_reads_environment(monkeypatch):
    monkeypatch.delenv("CTS_WIFI_PROVISIONING", raising=False)
    assert client.is_enabled() is False
    monkeypatch.setenv("CTS_WIFI_PROVISIONING", "1")
    assert client.is_enabled() is True


def test_request_raises_when_disabled(monkeypatch):
    monkeypatch.delenv("CTS_WIFI_PROVISIONING", raising=False)
    with pytest.raises(client.ProvisioningError, match="not enabled"):
        client.request("status")


def test_request_rejects_controller_owned_ap_network(monkeypatch):
    monkeypatch.setenv("CTS_WIFI_PROVISIONING", "1")
    with pytest.raises(client.ProvisioningError, match="controller-owned"):
        client.request("status", ap_network="192.168.4.0/24")


def test_request_round_trip_with_fake_socket(monkeypatch):
    path = Path(__file__).resolve().parents[1] / ".wifi-test.sock"
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)

    def serve():
        conn, _ = server.accept()
        with conn:
            payload = conn.recv(4096)
            request = json.loads(payload.decode("utf-8").strip())
            assert request == {"command": "status", "params": {}}
            conn.sendall(json.dumps({"success": True, "message": "ok", "status": {"state": "normal"}}).encode("utf-8") + b"\n")
        server.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    monkeypatch.setenv("CTS_WIFI_PROVISIONING", "1")
    monkeypatch.setenv("CTS_WIFI_SOCKET", str(path))

    try:
        response = client.request("status")
        assert response["success"] is True
        assert response["status"]["state"] == "normal"
        thread.join(timeout=1)
        assert not thread.is_alive()
    finally:
        server.close()
        path.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "payload",
    [
        {"success": "yes", "message": "ok"},
        {"success": True, "message": 9},
        {"success": True, "message": "ok", "status": []},
        [],
    ],
)
def test_request_rejects_invalid_response_envelope(monkeypatch, payload):
    class FakeSocket:
        def __init__(self, *args, **kwargs):
            self.response = json.dumps(payload).encode("utf-8") + b"\n"

        def settimeout(self, value):
            assert 0 < value <= client.DEFAULT_TIMEOUT

        def connect(self, path):
            assert path == client.DEFAULT_SOCKET

        def sendall(self, data):
            assert data.startswith(b'{"command":"status"')

        def recv(self, size):
            if not self.response:
                return b""
            data, self.response = self.response[:size], self.response[size:]
            return data

        def close(self):
            return None

    monkeypatch.setenv("CTS_WIFI_PROVISIONING", "1")
    monkeypatch.delenv("CTS_WIFI_SOCKET", raising=False)
    monkeypatch.setattr(client.socket, "socket", lambda *args, **kwargs: FakeSocket())

    with pytest.raises(client.ProvisioningError, match="Invalid provisioning response"):
        client.request("status")


def test_cli_main_sends_request_without_enabled_env(monkeypatch, capsys):
    monkeypatch.delenv("CTS_WIFI_PROVISIONING", raising=False)

    def fake_request(payload, *, require_enabled):
        assert payload == {"command": "enter_setup", "params": {}}
        assert require_enabled is False
        return {"success": True, "message": "ok", "status": {"state": "setup"}}

    monkeypatch.setattr(client, "_request_payload", fake_request)

    code = client.main(["enter_setup"])

    assert code == 0
    assert json.loads(capsys.readouterr().out) == {
        "success": True,
        "message": "ok",
        "status": {"state": "setup"},
    }


def test_cli_main_returns_nonzero_on_failure(monkeypatch, capsys):
    def fake_request(payload, *, require_enabled):
        raise client.ProvisioningError("socket unavailable")

    monkeypatch.setattr(client, "_request_payload", fake_request)

    code = client.main(["enter_setup"])

    assert code == 1
    assert json.loads(capsys.readouterr().err) == {
        "success": False,
        "message": "socket unavailable",
    }


def test_trickling_response_obeys_total_deadline(monkeypatch):
    now = [0.0]

    class SlowSocket:
        def settimeout(self, value):
            pass

        def connect(self, path):
            pass

        def sendall(self, data):
            pass

        def recv(self, size):
            now[0] += 1
            return b" "

        def close(self):
            pass

    monkeypatch.setenv("CTS_WIFI_PROVISIONING", "1")
    monkeypatch.setattr(client.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(client.socket, "socket", lambda *args: SlowSocket())
    with pytest.raises(client.ProvisioningError, match="timed out"):
        client.request("status")
    assert now[0] == client.DEFAULT_TIMEOUT
