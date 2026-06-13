"""Tests for the Phase 4 settings UI wiring:

* AzureRelayClient.update_relay_url
* /azure/config GET/POST
* /azure/status enriched payload
* load_settings legacy URL migration
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import azure_relay
import credentials_store
import CTS_Scoreboard
from azure_relay import (
    STATE_BACKOFF,
    STATE_CONNECTED,
    STATE_NEEDS_AUTH,
    AzureRelayClient,
)
from CTS_Scoreboard import app, settings


# --------------- update_relay_url ---------------


class TestUpdateRelayUrl:
    def _client(self, tmp_path=None):
        return AzureRelayClient(
            relay_url="https://old.example.com",
            creds_file="/tmp/_no.json",
        )

    def test_noop_when_unchanged(self):
        c = self._client()
        assert c.update_relay_url("https://old.example.com") is False

    def test_strips_whitespace(self):
        c = self._client()
        assert c.update_relay_url("  https://old.example.com  ") is False

    def test_swap_when_changed(self):
        c = self._client()
        assert c.update_relay_url("https://new.example.com") is True
        assert c.relay_url == "https://new.example.com"

    def test_does_not_force_reconnect_when_idle(self, monkeypatch):
        c = self._client()
        called = []
        monkeypatch.setattr(c, "force_reconnect", lambda: called.append(1))
        # Default state is STATE_NEEDS_AUTH; not in the reconnect set.
        with c._lock:
            c._set_state(STATE_NEEDS_AUTH)
        c.update_relay_url("https://changed.example.com")
        assert called == []

    def test_force_reconnect_when_connected(self, monkeypatch):
        c = self._client()
        called = []
        monkeypatch.setattr(c, "force_reconnect", lambda: called.append(1))
        with c._lock:
            c._set_state(STATE_CONNECTED)
        c.update_relay_url("https://changed.example.com")
        assert called == [1]


# --------------- HTTP routes (/azure/status, /azure/config) ---------------


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Point settings.json + azure_settings.json at temp files."""
    target = tmp_path / "settings.json"
    azure_target = tmp_path / "azure_settings.json"
    monkeypatch.setattr(CTS_Scoreboard, "settings_file", str(target))
    monkeypatch.setattr(CTS_Scoreboard, "azure_settings_file", str(azure_target))
    # Snapshot + restore the in-memory settings dict.
    snapshot = dict(settings)
    yield azure_target
    settings.clear()
    settings.update(snapshot)


@pytest.fixture
def logged_in_client(isolated_settings, monkeypatch):
    creds_path = str(isolated_settings.parent / "creds.json")
    monkeypatch.setattr(CTS_Scoreboard.azure_relay_client, "creds_file", creds_path)
    with CTS_Scoreboard.azure_relay_client._lock:
        CTS_Scoreboard.azure_relay_client._creds = None
        CTS_Scoreboard.azure_relay_client._set_state("needs_auth")
    app.config["TESTING"] = True
    with app.test_client() as c:
        c.post("/login", data={
            "username": credentials_store.DEFAULT_USERNAME,
            "password": credentials_store.DEFAULT_PASSWORD,
        })
        yield c


class TestAzureStatusEnriched:
    def test_includes_environment_and_urls(self, logged_in_client):
        settings["azure_environment"] = "preprod"
        settings["azure_relay_url_preprod"] = "https://preprod.example.com"
        settings["azure_public_url_preprod"] = "https://preprod-public.example.com"
        body = logged_in_client.get("/azure/status").get_json()
        assert body["environment"] == "preprod"
        assert body["relay_url"] == "https://preprod.example.com"
        assert body["public_url"] == "https://preprod-public.example.com"
        assert "enabled" in body

    def test_public_url_falls_back_to_relay(self, logged_in_client):
        settings["azure_environment"] = "preprod"
        settings["azure_relay_url_preprod"] = "https://relay.example.com"
        settings["azure_public_url_preprod"] = ""
        body = logged_in_client.get("/azure/status").get_json()
        assert body["public_url"] == "https://relay.example.com"


class TestAzureConfigGet:
    def test_returns_all_fields(self, logged_in_client):
        settings.update({
            "azure_environment": "prod",
            "azure_tenant_id": "tid",
            "azure_client_id": "cid",
            "azure_audience": "api://cid",
            "azure_relay_url_preprod": "https://pp.example.com",
            "azure_public_url_preprod": "",
            "azure_relay_url_prod": "https://prod.example.com",
            "azure_public_url_prod": "https://prod-public.example.com",
        })
        body = logged_in_client.get("/azure/config").get_json()
        assert body == {
            "environment": "prod",
            "tenant_id": "tid",
            "client_id": "cid",
            "audience": "api://cid",
            "relay_url_preprod": "https://pp.example.com",
            "public_url_preprod": "",
            "relay_url_prod": "https://prod.example.com",
            "public_url_prod": "https://prod-public.example.com",
        }


class TestAzureConfigPost:
    def test_rejects_bad_environment(self, logged_in_client):
        resp = logged_in_client.post("/azure/config", json={"environment": "staging"})
        assert resp.status_code == 400
        assert "environment" in resp.get_json()["error"]

    def test_rejects_bad_url_scheme(self, logged_in_client):
        resp = logged_in_client.post(
            "/azure/config",
            json={"relay_url_preprod": "ftp://nope.example.com"},
        )
        assert resp.status_code == 400
        assert "http" in resp.get_json()["error"]

    def test_persists_and_strips_trailing_slash(
        self, logged_in_client, isolated_settings
    ):
        resp = logged_in_client.post(
            "/azure/config",
            json={
                "environment": "preprod",
                "tenant_id": "tid-1",
                "relay_url_preprod": "https://pp.example.com/",
            },
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["status"]["environment"] == "preprod"
        assert body["status"]["relay_url"] == "https://pp.example.com"
        # Persisted to disk in azure_settings.json (not settings.json).
        on_disk = json.loads(isolated_settings.read_text())
        assert on_disk["azure_relay_url_preprod"] == "https://pp.example.com"
        assert on_disk["azure_tenant_id"] == "tid-1"

    def test_live_swaps_relay_url_on_active_env_change(
        self, logged_in_client, monkeypatch
    ):
        seen = []
        monkeypatch.setattr(
            CTS_Scoreboard.azure_relay_client,
            "update_relay_url",
            lambda url: seen.append(url) or True,
        )
        logged_in_client.post(
            "/azure/config",
            json={
                "environment": "prod",
                "relay_url_prod": "https://prod-new.example.com",
            },
        )
        assert seen == ["https://prod-new.example.com"]

    def test_empty_url_allowed(self, logged_in_client):
        resp = logged_in_client.post(
            "/azure/config",
            json={"relay_url_prod": ""},
        )
        assert resp.status_code == 200


# --------------- load_settings migration ---------------


class TestLegacyUrlMigration:
    def test_legacy_keys_migrate_to_preprod(self, tmp_path, monkeypatch):
        target = tmp_path / "settings.json"
        target.write_text(json.dumps({
            "azure_relay_url": "https://legacy.example.com",
            "azure_public_url": "https://legacy-public.example.com",
        }))
        azure_target = tmp_path / "azure_settings.json"
        monkeypatch.setattr(CTS_Scoreboard, "settings_file", str(target))
        monkeypatch.setattr(CTS_Scoreboard, "azure_settings_file", str(azure_target))
        snapshot = dict(settings)
        try:
            # Clear any operator-supplied values so the migration runs.
            for k in ("azure_relay_url_preprod", "azure_public_url_preprod"):
                settings[k] = ""
            CTS_Scoreboard.load_settings()
            assert settings["azure_relay_url_preprod"] == "https://legacy.example.com"
            assert settings["azure_public_url_preprod"] == "https://legacy-public.example.com"
            # The new file split now writes azure_* into azure_settings.json
            # and removes them from settings.json entirely.
            on_disk = json.loads(target.read_text())
            assert not any(k.startswith("azure_") for k in on_disk)
            azure_disk = json.loads(azure_target.read_text())
            assert azure_disk["azure_relay_url_preprod"] == "https://legacy.example.com"
        finally:
            settings.clear()
            settings.update(snapshot)

    def test_migration_does_not_overwrite_existing_preprod(
        self, tmp_path, monkeypatch
    ):
        target = tmp_path / "settings.json"
        target.write_text(json.dumps({
            "azure_relay_url": "https://legacy.example.com",
            "azure_relay_url_preprod": "https://already-set.example.com",
        }))
        azure_target = tmp_path / "azure_settings.json"
        monkeypatch.setattr(CTS_Scoreboard, "settings_file", str(target))
        monkeypatch.setattr(CTS_Scoreboard, "azure_settings_file", str(azure_target))
        snapshot = dict(settings)
        try:
            settings["azure_relay_url_preprod"] = ""
            CTS_Scoreboard.load_settings()
            assert (
                settings["azure_relay_url_preprod"]
                == "https://already-set.example.com"
            )
        finally:
            settings.clear()
            settings.update(snapshot)


class TestAzureSettingsFileSplit:
    def test_leaked_keys_migrate_out_of_settings_json(self, tmp_path, monkeypatch):
        """An older settings.json that still holds azure_* keys should have
        them moved into azure_settings.json on next load."""
        target = tmp_path / "settings.json"
        azure_target = tmp_path / "azure_settings.json"
        target.write_text(json.dumps({
            "meet_title": "u",
            "azure_tenant_id": "tid-x",
            "azure_client_id": "cid-x",
            "azure_environment": "preprod",
        }))
        monkeypatch.setattr(CTS_Scoreboard, "settings_file", str(target))
        monkeypatch.setattr(CTS_Scoreboard, "azure_settings_file", str(azure_target))
        snapshot = dict(settings)
        try:
            CTS_Scoreboard.load_settings()
            on_disk = json.loads(target.read_text())
            assert not any(k.startswith("azure_") for k in on_disk)
            azure_disk = json.loads(azure_target.read_text())
            assert azure_disk["azure_tenant_id"] == "tid-x"
            assert azure_disk["azure_client_id"] == "cid-x"
            assert azure_disk["azure_environment"] == "preprod"
        finally:
            settings.clear()
            settings.update(snapshot)

    def test_azure_settings_file_loaded_on_top_of_settings(
        self, tmp_path, monkeypatch
    ):
        target = tmp_path / "settings.json"
        azure_target = tmp_path / "azure_settings.json"
        target.write_text(json.dumps({"meet_title": "u"}))
        azure_target.write_text(json.dumps({
            "azure_tenant_id": "from-azure-file",
            "azure_environment": "prod",
        }))
        monkeypatch.setattr(CTS_Scoreboard, "settings_file", str(target))
        monkeypatch.setattr(CTS_Scoreboard, "azure_settings_file", str(azure_target))
        snapshot = dict(settings)
        try:
            CTS_Scoreboard.load_settings()
            assert settings["azure_tenant_id"] == "from-azure-file"
            assert settings["azure_environment"] == "prod"
        finally:
            settings.clear()
            settings.update(snapshot)

    def test_save_azure_settings_writes_only_azure_keys(self, tmp_path, monkeypatch):
        azure_target = tmp_path / "azure_settings.json"
        monkeypatch.setattr(CTS_Scoreboard, "azure_settings_file", str(azure_target))
        snapshot = dict(settings)
        try:
            settings["azure_tenant_id"] = "tid-out"
            settings["azure_client_id"] = "cid-out"
            settings["meet_title"] = "should-not-appear"
            CTS_Scoreboard.save_azure_settings()
            on_disk = json.loads(azure_target.read_text())
            assert on_disk["azure_tenant_id"] == "tid-out"
            assert on_disk["azure_client_id"] == "cid-out"
            assert "meet_title" not in on_disk
            assert all(k.startswith("azure_") for k in on_disk)
        finally:
            settings.clear()
            settings.update(snapshot)
