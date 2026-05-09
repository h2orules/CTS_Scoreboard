"""Tests for the Phase-3 _connect_and_serve loop using a fake socketio.Client."""
from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

import azure_relay
from azure_relay import (
    HEARTBEAT_DEGRADED_AFTER_S,
    STATE_CONNECTED,
    STATE_DEGRADED,
    STATE_STOPPED,
    AzureCredentials,
    AzureRelayClient,
    save_credentials,
)


def _sample_creds() -> AzureCredentials:
    return AzureCredentials(
        tenant_id="tid",
        client_id="cid",
        audience="api://aud",
        refresh_token="rt",
        account_id="loc",
        home_account_id="home",
        scopes=["api://aud/.default"],
        meet_id="abc123XYZ7890ab",
    )


class FakeSocketIOClient:
    """Minimal fake compatible with the subset of socketio.Client we use.

    Records emits, lets the test fire connect/disconnect/heartbeat_ack, and
    prevents real network I/O.
    """

    def __init__(self) -> None:
        self.connected = False
        self.handlers: dict[str, dict[str, Any]] = {}
        self.connect_calls: list[dict[str, Any]] = []
        self.emits: list[tuple[str, dict[str, Any]]] = []
        self._connect_event = threading.Event()

    def event(self, namespace: str = "/"):
        def deco(fn):
            self.handlers.setdefault(namespace, {})[fn.__name__] = fn
            return fn
        return deco

    def connect(self, url: str, *, namespaces=None, auth=None, transports=None, wait_timeout=10) -> None:
        self.connect_calls.append({"url": url, "auth": auth, "namespaces": namespaces})
        self.connected = True
        # Fire the registered connect handler synchronously.
        cb = self.handlers.get("/pi", {}).get("connect")
        if cb:
            cb()
        self._connect_event.set()

    def emit(self, name: str, payload: dict[str, Any], namespace: str = "/") -> None:
        self.emits.append((name, payload))

    def disconnect(self) -> None:
        self.connected = False
        cb = self.handlers.get("/pi", {}).get("disconnect")
        if cb:
            cb()

    def fire_ack(self, data: dict[str, Any]) -> None:
        cb = self.handlers["/pi"]["heartbeat_ack"]
        cb(data)


@pytest.fixture
def relay_with_fake_client(tmp_path):
    """Build a relay client with a fake socketio client and msal factory."""
    creds_path = str(tmp_path / "creds.json")
    save_credentials(creds_path, _sample_creds())

    fake_msal = MagicMock()
    fake_msal.acquire_token_by_refresh_token.return_value = {"access_token": "fresh-at"}

    fake_sock = FakeSocketIOClient()

    client = AzureRelayClient(
        creds_file=creds_path,
        relay_url="https://relay.example.com",
        backoff_schedule=(0, 0, 0),
        msal_app_factory=lambda **_: fake_msal,
    )
    # Inject the fake socket factory.
    client._socketio_client_factory = lambda: fake_sock  # type: ignore[method-assign]
    return client, fake_sock, fake_msal


class TestConnectAndServeHandshake:
    def test_meet_open_emitted_with_protocol_version(self, relay_with_fake_client):
        client, fake_sock, _ = relay_with_fake_client
        client.start()
        # Wait for connect.
        deadline = time.time() + 2.0
        while time.time() < deadline and client.status != STATE_CONNECTED:
            time.sleep(0.02)
        try:
            assert client.status == STATE_CONNECTED
            assert fake_sock.connect_calls
            auth = fake_sock.connect_calls[0]["auth"]
            assert auth["meet_id"] == "abc123XYZ7890ab"
            assert auth["access_token"] == "fresh-at"
            assert auth["protocol_version"] == 1
            # First emit is meet_open.
            names = [e[0] for e in fake_sock.emits]
            assert names[0] == "meet_open"
            assert fake_sock.emits[0][1]["meet_id"] == "abc123XYZ7890ab"
        finally:
            client.stop(timeout=1.0)

    def test_forwarded_events_are_emitted(self, relay_with_fake_client):
        client, fake_sock, _ = relay_with_fake_client
        client.start()
        deadline = time.time() + 2.0
        while time.time() < deadline and client.status != STATE_CONNECTED:
            time.sleep(0.02)
        try:
            client.forward_event("update_scoreboard", {"foo": 1})
            time.sleep(0.7)  # > queue.get timeout (0.5s)
            names = [e[0] for e in fake_sock.emits]
            assert "update_scoreboard" in names
        finally:
            client.stop(timeout=1.0)


class TestTemplatePush:
    def test_bundle_pushed_after_meet_open(self, tmp_path):
        creds_path = str(tmp_path / "creds.json")
        save_credentials(creds_path, _sample_creds())

        fake_msal = MagicMock()
        fake_msal.acquire_token_by_refresh_token.return_value = {"access_token": "fresh-at"}
        fake_sock = FakeSocketIOClient()

        bundle = {"bundle_id": "deadbeef", "template_path": "web/home.html",
                  "template_text": "<html></html>", "static_files": {}, "partial_files": {}}

        client = AzureRelayClient(
            creds_file=creds_path,
            relay_url="https://relay.example.com",
            backoff_schedule=(0, 0, 0),
            msal_app_factory=lambda **_: fake_msal,
            bundle_provider=lambda: bundle,
        )
        client._socketio_client_factory = lambda: fake_sock  # type: ignore[method-assign]
        client.start()
        deadline = time.time() + 2.0
        while time.time() < deadline and client.status != STATE_CONNECTED:
            time.sleep(0.02)
        try:
            names = [e[0] for e in fake_sock.emits]
            assert "meet_open" in names
            assert "template_push" in names
            # template_push comes after meet_open.
            assert names.index("template_push") > names.index("meet_open")
            # Bundle payload propagates verbatim.
            push = next(e for e in fake_sock.emits if e[0] == "template_push")
            assert push[1]["bundle_id"] == "deadbeef"
        finally:
            client.stop(timeout=1.0)


class TestRefreshTokenFailure:
    def test_refresh_token_rejected_triggers_backoff(self, tmp_path):
        creds_path = str(tmp_path / "creds.json")
        save_credentials(creds_path, _sample_creds())

        fake_msal = MagicMock()
        fake_msal.acquire_token_by_refresh_token.return_value = {
            "error": "invalid_grant",
            "error_description": "refresh token expired",
        }
        client = AzureRelayClient(
            creds_file=creds_path,
            relay_url="https://relay.example.com",
            backoff_schedule=(0, 0, 0),
            msal_app_factory=lambda **_: fake_msal,
        )
        client._socketio_client_factory = lambda: FakeSocketIOClient()  # type: ignore[method-assign]
        client.start()
        deadline = time.time() + 2.0
        while time.time() < deadline and client.snapshot()["attempt"] < 1:
            time.sleep(0.02)
        client.stop(timeout=1.0)
        snap = client.snapshot()
        assert snap["attempt"] >= 1
        assert "refresh" in (snap["last_error"] or "")
