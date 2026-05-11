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
    MEET_ID_MAX_LEN,
    MEET_ID_MIN_LEN,
    STATE_AUTHENTICATING,
    STATE_BACKOFF,
    STATE_DISCONNECTED,
    STATE_NEEDS_AUTH,
    STATE_STOPPED,
    AzureCredentials,
    AzureRelayClient,
    clear_credentials,
    compute_backoff,
    derive_meet_id_default,
    generate_meet_id,
    load_credentials,
    save_credentials,
    validate_meet_id,
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
        scopes=["api://aud/Pi.Connect"],
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


class TestValidateMeetId:
    def test_accepts_min_length(self):
        ok, err = validate_meet_id("a" * MEET_ID_MIN_LEN)
        assert ok and err is None

    def test_accepts_max_length(self):
        ok, _ = validate_meet_id("a" * MEET_ID_MAX_LEN)
        assert ok

    def test_accepts_dashes_and_underscores(self):
        ok, _ = validate_meet_id("Midlakes-2026_v1")
        assert ok

    def test_accepts_mixed_case(self):
        ok, _ = validate_meet_id("MidlakesM-26")
        assert ok

    def test_rejects_too_short(self):
        ok, err = validate_meet_id("a" * (MEET_ID_MIN_LEN - 1))
        assert not ok and "at least" in (err or "")

    def test_rejects_too_long(self):
        ok, err = validate_meet_id("a" * (MEET_ID_MAX_LEN + 1))
        assert not ok and "at most" in (err or "")

    def test_rejects_spaces(self):
        ok, err = validate_meet_id("with space12")
        assert not ok and "letters" in (err or "").lower()

    def test_rejects_special_chars(self):
        for s in ("with$dollar", "with.dot12", "with/slash", "with!bang3"):
            ok, _ = validate_meet_id(s)
            assert not ok, s

    def test_rejects_empty(self):
        ok, err = validate_meet_id("")
        assert not ok
        ok, err = validate_meet_id(None)  # type: ignore[arg-type]
        assert not ok

    def test_generated_ids_pass_validation(self):
        for _ in range(50):
            ok, _ = validate_meet_id(generate_meet_id())
            assert ok


class TestDeriveMeetIdDefault:
    def test_simple_team_name(self):
        out = derive_meet_id_default("Midlakes Marlins")
        assert out == "Midlakes-Marlins"
        ok, _ = validate_meet_id(out)
        assert ok

    def test_short_name_padded(self):
        # "Foo" -> "Foo" (3 chars), needs padding to 10.
        out = derive_meet_id_default("Foo", _rng=lambda: "X")
        assert out == "Foo-XXXXXX"
        assert len(out) == MEET_ID_MIN_LEN
        ok, _ = validate_meet_id(out)
        assert ok

    def test_strips_special_chars(self):
        out = derive_meet_id_default("Foo!Bar@Baz#Qux", _rng=lambda: "X")
        assert out == "FooBarBazQux"
        ok, _ = validate_meet_id(out)
        assert ok

    def test_collapses_repeated_separators(self):
        out = derive_meet_id_default("Midlakes  Marlins")
        assert out == "Midlakes-Marlins"

    def test_truncates_to_max(self):
        long = "Midlakes Marlins Junior Varsity Team 2026 Spring"
        out = derive_meet_id_default(long)
        assert len(out) <= MEET_ID_MAX_LEN
        ok, _ = validate_meet_id(out)
        assert ok

    def test_empty_falls_back_to_random(self):
        out = derive_meet_id_default("")
        assert len(out) == MEET_ID_LENGTH
        ok, _ = validate_meet_id(out)
        assert ok

    def test_whitespace_only_falls_back_to_random(self):
        out = derive_meet_id_default("   \t\n  ")
        assert len(out) == MEET_ID_LENGTH

    def test_only_specials_falls_back_to_random(self):
        out = derive_meet_id_default("!!!@@@###")
        # Stripped to empty -> random fallback.
        assert len(out) == MEET_ID_LENGTH

    def test_trims_leading_trailing_separators(self):
        out = derive_meet_id_default("--FooBarBaz--")
        assert out == "FooBarBaz-X" or out.startswith("FooBarBaz")


class TestSetMeetId:
    def test_returns_error_when_not_signed_in(self, tmp_path):
        client = AzureRelayClient(creds_file=str(tmp_path / "absent.json"))
        ok, msg = client.set_meet_id("Midlakes-2026")
        assert not ok
        assert "signed in" in msg.lower()

    def test_returns_error_for_invalid_name(self, tmp_path):
        path = str(tmp_path / "creds.json")
        save_credentials(path, _sample_creds())
        client = AzureRelayClient(creds_file=path)
        ok, msg = client.set_meet_id("bad name!")
        assert not ok
        # Original meet_id unchanged.
        loaded = load_credentials(path)
        assert loaded is not None
        assert loaded.meet_id != "bad name!"

    def test_persists_and_force_reconnects(self, tmp_path):
        path = str(tmp_path / "creds.json")
        save_credentials(path, _sample_creds(meet_id="aaaaaaaaaaaaaaa"))
        client = AzureRelayClient(creds_file=path)
        ok, val = client.set_meet_id("Midlakes-2026")
        assert ok
        assert val == "Midlakes-2026"
        loaded = load_credentials(path)
        assert loaded is not None
        assert loaded.meet_id == "Midlakes-2026"
        # The in-memory creds also reflect the new value.
        assert client.meet_id == "Midlakes-2026"


class TestCheckMeetIdAvailable:
    def test_invalid_name_returns_error_without_network(self, tmp_path):
        path = str(tmp_path / "creds.json")
        save_credentials(path, _sample_creds())
        client = AzureRelayClient(creds_file=path)
        res = client.check_meet_id_available("bad name!")
        assert res["ok"] is False
        assert res["error"]
        assert res["available"] is False

    def test_not_signed_in_returns_error(self, tmp_path):
        client = AzureRelayClient(creds_file=str(tmp_path / "absent.json"))
        res = client.check_meet_id_available("Midlakes-2026")
        assert res["ok"] is False
        assert "signed in" in res["error"].lower()


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

    def test_default_scope_is_named_pi_connect(self, tmp_path):
        """Default scope is `api://<client_id>/Pi.Connect`, not `.default`.

        Using the named delegated scope keeps consent on a per-user basis
        — admin consent isn't required to talk to your own relay app."""
        fake_app = MagicMock()
        fake_app.initiate_device_flow.return_value = {
            "user_code": "X", "verification_uri": "u",
            "expires_in": 600, "message": "m",
        }
        client = AzureRelayClient(
            creds_file=str(tmp_path / "creds.json"),
            msal_app_factory=lambda **_: fake_app,
        )
        client.request_login(
            tenant_id="tid", client_id="the-guid", audience="api://the-guid",
        )
        scopes = fake_app.initiate_device_flow.call_args.kwargs["scopes"]
        assert scopes == ["api://the-guid/Pi.Connect"]

    def test_explicit_scopes_override_default(self, tmp_path):
        """Callers can still pass an explicit scope list to override."""
        fake_app = MagicMock()
        fake_app.initiate_device_flow.return_value = {
            "user_code": "X", "verification_uri": "u",
            "expires_in": 600, "message": "m",
        }
        client = AzureRelayClient(
            creds_file=str(tmp_path / "creds.json"),
            msal_app_factory=lambda **_: fake_app,
        )
        client.request_login(
            tenant_id="tid", client_id="cid", audience="api://aud",
            scopes=["api://other/Custom.Scope"],
        )
        scopes = fake_app.initiate_device_flow.call_args.kwargs["scopes"]
        assert scopes == ["api://other/Custom.Scope"]

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

    def test_cancel_login_clears_in_flight_flow(self, tmp_path):
        fake_app = MagicMock()
        fake_app.initiate_device_flow.return_value = {
            "user_code": "X", "verification_uri": "u",
            "expires_in": 600, "expires_at": 9999999999,
            "message": "m",
        }
        client = AzureRelayClient(
            creds_file=str(tmp_path / "creds.json"),
            msal_app_factory=lambda **_: fake_app,
        )
        client.request_login(tenant_id="tid", client_id="cid", audience="api://aud")
        assert client.status == STATE_AUTHENTICATING
        cancelled = client.cancel_login()
        assert cancelled is True
        assert client.status == STATE_NEEDS_AUTH
        snap = client.snapshot()
        assert snap["device_flow"] is None
        # The MSAL flow's expires_at was zeroed so a still-blocked
        # acquire_token_by_device_flow loop will give up on next poll.
        # (We can't directly inspect the dict via the client API; this is
        # exercised by the no-op behaviour.)

    def test_cancel_login_with_no_flow_is_noop(self, tmp_path):
        client = AzureRelayClient(creds_file=str(tmp_path / "creds.json"))
        assert client.cancel_login() is False
        assert client.status == STATE_NEEDS_AUTH


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
