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
