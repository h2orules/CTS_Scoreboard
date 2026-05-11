"""Smoke tests for /azure/* settings routes (Phase 2 wiring)."""
from __future__ import annotations

import json
import os

import pytest

import CTS_Scoreboard
from CTS_Scoreboard import app, settings


@pytest.fixture
def logged_in_client(tmp_path, monkeypatch):
    # Point the relay at a temp creds file so tests don't touch real creds.
    creds_path = str(tmp_path / "creds.json")
    monkeypatch.setattr(CTS_Scoreboard.azure_relay_client, "creds_file", creds_path)
    # Reset the in-memory state to a clean signed-out state.
    with CTS_Scoreboard.azure_relay_client._lock:
        CTS_Scoreboard.azure_relay_client._creds = None
        CTS_Scoreboard.azure_relay_client._set_state("needs_auth")

    app.config["TESTING"] = True
    with app.test_client() as c:
        c.post("/login", data={
            "username": settings["username"],
            "password": settings["password"],
        })
        yield c


class TestAzureStatus:
    def test_returns_snapshot_json(self, logged_in_client):
        resp = logged_in_client.get("/azure/status")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["state"] == "needs_auth"
        assert body["meet_id"] is None
        assert "protocol_version" in body

    def test_requires_login(self):
        app.config["TESTING"] = True
        with app.test_client() as c:
            resp = c.get("/azure/status")
            assert resp.status_code in (302, 401)


class TestAzureLogin:
    def test_login_requires_credentials(self, logged_in_client, monkeypatch):
        # Clear any operator-supplied values so the empty POST body is what
        # actually drives the validation path.
        for k in ('azure_tenant_id', 'azure_client_id', 'azure_audience'):
            monkeypatch.setitem(settings, k, '')
        resp = logged_in_client.post("/azure/login", json={})
        assert resp.status_code == 400
        assert "tenant_id" in resp.get_json()["error"]


class TestAzureRotateId:
    def test_rotate_when_not_signed_in_returns_400(self, logged_in_client):
        resp = logged_in_client.post("/azure/rotate_id")
        assert resp.status_code == 400


class TestAzureLogoutWhenNotSignedIn:
    def test_logout_is_idempotent(self, logged_in_client):
        resp = logged_in_client.post("/azure/logout")
        assert resp.status_code == 200
        assert resp.get_json()["status"]["state"] == "needs_auth"


class TestAzureSetMeetId:
    def test_rejects_invalid_name(self, logged_in_client):
        resp = logged_in_client.post("/azure/set_meet_id", json={"name": "bad name!"})
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["ok"] is False
        assert body["error"]

    def test_rejects_when_not_signed_in(self, logged_in_client):
        # Valid name format, but the client has no creds in the fixture state.
        resp = logged_in_client.post("/azure/set_meet_id",
                                     json={"name": "Midlakes-2026"})
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["ok"] is False
        assert "signed in" in body["error"].lower()

    def test_accepts_valid_name_when_signed_in(self, logged_in_client):
        # Inject minimal fake credentials so set_meet_id() reaches persistence.
        from azure_relay import AzureCredentials
        with CTS_Scoreboard.azure_relay_client._lock:
            CTS_Scoreboard.azure_relay_client._creds = AzureCredentials(
                tenant_id="tid", client_id="cid", audience="api://aud",
                refresh_token="rt", account_id="oid", home_account_id="hoid",
                meet_id="oldOldOld12345",
            )
        resp = logged_in_client.post("/azure/set_meet_id",
                                     json={"name": "Midlakes-2026"})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["meet_id"] == "Midlakes-2026"


class TestAzureCheckMeetId:
    def test_invalid_name_returns_error_without_network(self, logged_in_client):
        resp = logged_in_client.post("/azure/check_meet_id", json={"name": "bad!"})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is False
        assert body["available"] is False
        assert body["error"]

    def test_not_signed_in_returns_error(self, logged_in_client):
        resp = logged_in_client.post("/azure/check_meet_id",
                                     json={"name": "Midlakes-2026"})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is False
        assert "signed in" in body["error"].lower()
