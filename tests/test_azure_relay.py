"""Tests for the Pi-side AzureRelayClient (state machine, backoff, msal flow).

Phase 2: covers everything except the actual Socket.IO connection (which is a
seam in ``_connect_and_serve`` that Phase 3 fills in).
"""
from __future__ import annotations

import json
import os
import time
from unittest.mock import MagicMock

import pytest

import azure_relay
from azure_relay import (
    BACKOFF_SCHEDULE,
    MEET_ID_ALPHABET,
    MEET_ID_LENGTH,
    STATE_AUTHENTICATING,
    STATE_BACKOFF,
    STATE_DISCONNECTED,
    STATE_NEEDS_AUTH,
    STATE_STOPPED,
    AzureCredentials,
    AzureRelayClient,
    clear_credentials,
    compute_backoff,
    generate_meet_id,
    load_credentials,
    save_credentials,
)


# ---------------- pure helpers ----------------


class TestMeetId:
    def test_length(self):
        assert len(generate_meet_id()) == MEET_ID_LENGTH

    def test_alphabet(self):
        for _ in range(50):
            mid = generate_meet_id()
            assert all(c in MEET_ID_ALPHABET for c in mid)

    def test_no_ambiguous_chars(self):
        # No 0/O/1/l/I.
        for _ in range(200):
            mid = generate_meet_id()
            assert "0" not in mid
            assert "O" not in mid
            assert "1" not in mid
            assert "l" not in mid
            assert "I" not in mid

    def test_uniqueness_strong(self):
        ids = {generate_meet_id() for _ in range(2000)}
        assert len(ids) == 2000  # collision-free at this scale


class TestBackoff:
    def test_schedule_sequence(self):
        assert compute_backoff(0) == 1
        assert compute_backoff(1) == 2
        assert compute_backoff(2) == 4
        assert compute_backoff(3) == 8

    def test_caps_at_last(self):
        assert compute_backoff(99) == BACKOFF_SCHEDULE[-1]
        assert compute_backoff(99) == 300

    def test_negative_clamped(self):
        assert compute_backoff(-5) == BACKOFF_SCHEDULE[0]


# ---------------- credential persistence ----------------


def _sample_creds(meet_id: str = "abc123XYZ7890ab") -> AzureCredentials:
    return AzureCredentials(
        tenant_id="tid",
        client_id="cid",
        audience="aud",
        refresh_token="r-token",
        account_id="acct-local",
        home_account_id="acct-home",
        upn="user@example.com",
        scopes=["api://aud/.default"],
        meet_id=meet_id,
    )


class TestCredentialPersistence:
    def test_save_and_load_round_trip(self, tmp_path):
        path = str(tmp_path / "creds.json")
        creds = _sample_creds()
        save_credentials(path, creds)
        loaded = load_credentials(path)
        assert loaded == creds

    def test_save_uses_0600_mode(self, tmp_path):
        path = str(tmp_path / "creds.json")
        save_credentials(path, _sample_creds())
        if hasattr(os, "stat"):
            mode = os.stat(path).st_mode & 0o777
            # Skip on platforms (e.g. Windows test envs) that ignore chmod.
            if mode != 0o644:  # not just whatever the umask gave us
                assert mode == 0o600

    def test_load_missing_returns_none(self, tmp_path):
        assert load_credentials(str(tmp_path / "absent.json")) is None

    def test_load_corrupt_returns_none(self, tmp_path):
        path = str(tmp_path / "bad.json")
        with open(path, "w") as f:
            f.write("not json {{{")
        assert load_credentials(path) is None

    def test_load_partial_returns_none(self, tmp_path):
        # Missing required field -> reconstruction fails -> None.
        path = str(tmp_path / "partial.json")
        with open(path, "w") as f:
            json.dump({"tenant_id": "t"}, f)
        assert load_credentials(path) is None

    def test_clear_removes_file(self, tmp_path):
        path = str(tmp_path / "creds.json")
        save_credentials(path, _sample_creds())
        assert os.path.exists(path)
        clear_credentials(path)
        assert not os.path.exists(path)

    def test_clear_missing_is_noop(self, tmp_path):
        # Should not raise.
        clear_credentials(str(tmp_path / "absent.json"))


# ---------------- relay client state machine ----------------


class TestRelayClientInitialState:
    def test_no_creds_starts_in_needs_auth(self, tmp_path):
        path = str(tmp_path / "absent.json")
        client = AzureRelayClient(creds_file=path)
        assert client.status == STATE_NEEDS_AUTH
        assert client.meet_id is None

    def test_with_creds_starts_in_disconnected(self, tmp_path):
        path = str(tmp_path / "creds.json")
        save_credentials(path, _sample_creds(meet_id="abcdefghijk1234"))
        client = AzureRelayClient(creds_file=path)
        assert client.status == STATE_DISCONNECTED
        assert client.meet_id == "abcdefghijk1234"


class TestSnapshot:
    def test_snapshot_includes_required_keys(self, tmp_path):
        client = AzureRelayClient(creds_file=str(tmp_path / "absent.json"))
        snap = client.snapshot()
        for key in (
            "state",
            "meet_id",
            "upn",
            "last_error",
            "last_connected_at",
            "last_heartbeat_at",
            "next_retry_at",
            "attempt",
            "active_client_count",
            "protocol_version",
            "device_flow",
        ):
            assert key in snap
        # JSON-serializable.
        json.dumps(snap)


class TestStatusSubscribers:
    def test_subscriber_fires_on_state_change(self, tmp_path):
        client = AzureRelayClient(creds_file=str(tmp_path / "absent.json"))
        events: list[dict] = []
        client.subscribe_status(events.append)
        client._set_state(STATE_DISCONNECTED)
        assert events
        assert events[-1]["state"] == STATE_DISCONNECTED

    def test_subscriber_does_not_fire_on_no_change(self, tmp_path):
        client = AzureRelayClient(creds_file=str(tmp_path / "absent.json"))
        events: list[dict] = []
        client.subscribe_status(events.append)
        client._set_state(STATE_NEEDS_AUTH)  # already in this state
        assert events == []

    def test_subscriber_exception_does_not_break_state_change(self, tmp_path):
        client = AzureRelayClient(creds_file=str(tmp_path / "absent.json"))
        client.subscribe_status(lambda _: (_ for _ in ()).throw(RuntimeError("boom")))
        client._set_state(STATE_DISCONNECTED)
        assert client.status == STATE_DISCONNECTED


# ---------------- backoff loop integration ----------------


class TestBackoffLoop:
    def test_backoff_state_set_after_failed_connect(self, tmp_path, monkeypatch):
        path = str(tmp_path / "creds.json")
        save_credentials(path, _sample_creds())

        # Use a tight backoff schedule so the test runs fast. Empty relay_url
        # makes _connect_and_serve raise immediately ("relay_url is not
        # configured") so we don't hit the network.
        client = AzureRelayClient(
            creds_file=path,
            relay_url="",
            backoff_schedule=(0, 0, 0),
        )
        client.start()
        # Give the loop a moment to attempt + fail.
        deadline = time.time() + 2.0
        while time.time() < deadline and client.snapshot()["attempt"] < 1:
            time.sleep(0.05)
        client.stop(timeout=1.0)
        assert client.status == STATE_STOPPED
        snap = client.snapshot()
        assert snap["attempt"] >= 1
        assert "relay_url" in (snap["last_error"] or "")


class TestForceReconnect:
    def test_clears_backoff(self, tmp_path):
        path = str(tmp_path / "creds.json")
        save_credentials(path, _sample_creds())
        client = AzureRelayClient(creds_file=path)
        # Simulate being in backoff.
        with client._lock:
            client._attempt = 5
            client._next_retry_at = time.time() + 9999
            client._set_state(STATE_BACKOFF)
        client.force_reconnect()
        snap = client.snapshot()
        assert snap["attempt"] == 0
        assert snap["next_retry_at"] is None


class TestRotateMeetId:
    def test_returns_none_when_not_signed_in(self, tmp_path):
        client = AzureRelayClient(creds_file=str(tmp_path / "absent.json"))
        assert client.rotate_meet_id() is None

    def test_changes_id_and_persists(self, tmp_path):
        path = str(tmp_path / "creds.json")
        save_credentials(path, _sample_creds(meet_id="aaaaaaaaaaaaaaa"))
        client = AzureRelayClient(creds_file=path)
        old = client.meet_id
        new = client.rotate_meet_id()
        assert new is not None
        assert new != old
        # Persisted to disk.
        loaded = load_credentials(path)
        assert loaded is not None
        assert loaded.meet_id == new


class TestLogout:
    def test_clears_creds_and_file(self, tmp_path):
        path = str(tmp_path / "creds.json")
        save_credentials(path, _sample_creds())
        client = AzureRelayClient(creds_file=path)
        client.logout()
        assert client.status == STATE_NEEDS_AUTH
        assert client.meet_id is None
        assert not os.path.exists(path)


# ---------------- msal device-code flow (mocked) ----------------


class TestDeviceCodeFlow:
    def test_request_login_returns_user_code(self, tmp_path):
        fake_app = MagicMock()
        fake_app.initiate_device_flow.return_value = {
            "user_code": "AB12-CD34",
            "verification_uri": "https://microsoft.com/devicelogin",
            "expires_in": 600,
            "message": "Visit URL and enter the code.",
        }
        client = AzureRelayClient(
            creds_file=str(tmp_path / "creds.json"),
            msal_app_factory=lambda **_: fake_app,
        )
        flow = client.request_login(
            tenant_id="tid", client_id="cid", audience="api://aud"
        )
        assert flow.user_code == "AB12-CD34"
        assert flow.verification_uri == "https://microsoft.com/devicelogin"
        assert client.status == STATE_AUTHENTICATING
        snap = client.snapshot()
        assert snap["device_flow"]["user_code"] == "AB12-CD34"

    def test_complete_login_persists_creds(self, tmp_path):
        fake_app = MagicMock()
        fake_app.initiate_device_flow.return_value = {
            "user_code": "X",
            "verification_uri": "u",
            "expires_in": 600,
            "message": "m",
        }
        fake_app.acquire_token_by_device_flow.return_value = {
            "access_token": "at",
            "expires_in": 3600,
        }
        fake_app.get_accounts.return_value = [
            {"local_account_id": "loc", "home_account_id": "home", "username": "u@x"}
        ]
        # Real msal serializes the cache; mock it to inject a refresh token.
        fake_app.token_cache.serialize.return_value = json.dumps(
            {"RefreshToken": {"k": {"secret": "rt-secret"}}}
        )
        path = str(tmp_path / "creds.json")
        client = AzureRelayClient(
            creds_file=path, msal_app_factory=lambda **_: fake_app
        )
        client.request_login(tenant_id="tid", client_id="cid", audience="api://aud")
        ok = client.complete_login()
        assert ok is True
        loaded = load_credentials(path)
        assert loaded is not None
        assert loaded.refresh_token == "rt-secret"
        assert loaded.upn == "u@x"
        assert loaded.tenant_id == "tid"
        assert loaded.client_id == "cid"
        assert loaded.audience == "api://aud"
        assert len(loaded.meet_id) == MEET_ID_LENGTH

    def test_complete_login_failure_returns_to_needs_auth(self, tmp_path):
        fake_app = MagicMock()
        fake_app.initiate_device_flow.return_value = {
            "user_code": "X",
            "verification_uri": "u",
            "expires_in": 600,
            "message": "m",
        }
        fake_app.acquire_token_by_device_flow.return_value = {
            "error": "authorization_declined",
            "error_description": "user cancelled",
        }
        client = AzureRelayClient(
            creds_file=str(tmp_path / "creds.json"),
            msal_app_factory=lambda **_: fake_app,
        )
        client.request_login(tenant_id="tid", client_id="cid", audience="api://aud")
        ok = client.complete_login()
        assert ok is False
        assert client.status == STATE_NEEDS_AUTH
        assert client.snapshot()["last_error"] is not None

    def test_request_login_raises_on_initiate_failure(self, tmp_path):
        fake_app = MagicMock()
        fake_app.initiate_device_flow.return_value = {
            "error": "invalid_client",
            "error_description": "bad client",
        }
        client = AzureRelayClient(
            creds_file=str(tmp_path / "creds.json"),
            msal_app_factory=lambda **_: fake_app,
        )
        with pytest.raises(RuntimeError):
            client.request_login(
                tenant_id="tid", client_id="cid", audience="api://aud"
            )


# ---------------- forward_event ----------------


class TestForwardEvent:
    def test_enqueues_event(self, tmp_path):
        client = AzureRelayClient(creds_file=str(tmp_path / "absent.json"))
        assert client.forward_event("update_scoreboard", {"x": 1}) is True
        assert client._queue.qsize() == 1

    def test_returns_false_when_full(self, tmp_path):
        client = AzureRelayClient(creds_file=str(tmp_path / "absent.json"))
        # Queue is bounded at 1000; fill it.
        for _ in range(1000):
            client._queue.put_nowait(("e", {}))
        assert client.forward_event("e", {}) is False
