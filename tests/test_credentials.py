"""Tests for the hashed web-login credential store (credentials_store.py)."""
import json
import os
import stat

import pytest

import credentials_store
import CTS_Scoreboard
from CTS_Scoreboard import app, settings


@pytest.fixture
def logged_in_client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        c.post("/login", data={
            "username": credentials_store.DEFAULT_USERNAME,
            "password": credentials_store.DEFAULT_PASSWORD,
        })
        yield c


class TestCredentialsStore:
    def test_save_and_verify_roundtrip(self):
        credentials_store.save_credentials("alice", "s3cret")
        assert credentials_store.verify_login("alice", "s3cret")
        assert not credentials_store.verify_login("alice", "wrong")
        assert not credentials_store.verify_login("bob", "s3cret")

    def test_defaults_when_store_absent(self):
        assert not os.path.exists(credentials_store.credentials_file)
        assert credentials_store.get_username() == "admin"
        assert credentials_store.verify_login("admin", "password")
        assert not credentials_store.verify_login("admin", "nope")

    def test_store_contains_no_plaintext(self):
        credentials_store.save_credentials("alice", "s3cret")
        with open(credentials_store.credentials_file) as f:
            raw = f.read()
        assert "s3cret" not in raw
        store = json.loads(raw)
        assert store["algorithm"] == "pbkdf2_sha256"
        bytes.fromhex(store["salt"])
        bytes.fromhex(store["password_hash"])
        assert isinstance(store["iterations"], int)

    def test_fresh_salt_per_save(self):
        credentials_store.save_credentials("alice", "s3cret")
        first = credentials_store.load_store()
        credentials_store.save_credentials("alice", "s3cret")
        second = credentials_store.load_store()
        assert first["salt"] != second["salt"]
        assert first["password_hash"] != second["password_hash"]

    def test_file_mode_is_0600(self):
        credentials_store.save_credentials("alice", "s3cret")
        mode = stat.S_IMODE(os.stat(credentials_store.credentials_file).st_mode)
        assert mode == 0o600

    def test_set_username_without_store_keeps_default_password(self):
        credentials_store.set_username("alice")
        assert credentials_store.verify_login(
            "alice", credentials_store.DEFAULT_PASSWORD)

    def test_set_username_with_store_keeps_password(self):
        credentials_store.save_credentials("alice", "s3cret")
        credentials_store.set_username("bob")
        assert credentials_store.verify_login("bob", "s3cret")
        assert not credentials_store.verify_login("alice", "s3cret")

    def test_set_password_keeps_username(self):
        credentials_store.save_credentials("alice", "s3cret")
        credentials_store.set_password("newpw")
        assert credentials_store.verify_login("alice", "newpw")
        assert not credentials_store.verify_login("alice", "s3cret")

    def test_corrupt_store_falls_back_to_defaults(self):
        with open(credentials_store.credentials_file, "wt") as f:
            f.write("not json")
        assert credentials_store.get_username() == "admin"
        # A corrupt store falls back to the defaults rather than locking out.
        assert credentials_store.verify_login("admin", "password")


class TestCredentialMigration:
    def test_plaintext_settings_migrate_to_store(self, monkeypatch):
        target = CTS_Scoreboard.settings_file
        with open(target, "wt") as f:
            json.dump({"username": "legacy", "password": "oldpw"}, f)
        snapshot = dict(settings)
        try:
            CTS_Scoreboard.load_settings()
            assert credentials_store.verify_login("legacy", "oldpw")
            assert "username" not in settings
            assert "password" not in settings
            with open(target) as f:
                on_disk = json.load(f)
            assert "username" not in on_disk
            assert "password" not in on_disk
        finally:
            settings.clear()
            settings.update(snapshot)

    def test_migration_does_not_overwrite_existing_store(self):
        credentials_store.save_credentials("alice", "s3cret")
        with open(CTS_Scoreboard.settings_file, "wt") as f:
            json.dump({"username": "stale", "password": "stalepw"}, f)
        snapshot = dict(settings)
        try:
            CTS_Scoreboard.load_settings()
            assert credentials_store.verify_login("alice", "s3cret")
            assert not credentials_store.verify_login("stale", "stalepw")
            assert "username" not in settings
        finally:
            settings.clear()
            settings.update(snapshot)


class TestCredentialRoutes:
    def test_login_against_saved_store(self):
        credentials_store.save_credentials("alice", "s3cret")
        app.config["TESTING"] = True
        with app.test_client() as c:
            resp = c.post("/login?next=/settings", data={
                "username": "alice", "password": "s3cret"})
            assert resp.status_code == 302
            assert c.get("/settings").status_code == 200

    def test_login_rejects_bad_password(self):
        app.config["TESTING"] = True
        with app.test_client() as c:
            # The 401 handler re-renders the login page with the failure note.
            resp = c.post("/login", data={
                "username": "admin", "password": "wrong"})
            assert b"Login failed" in resp.data
            # Still unauthenticated: /settings bounces to the login page.
            resp = c.get("/settings")
            assert resp.status_code == 302
            assert "/login" in resp.headers["Location"]

    def test_change_password_via_settings(self, logged_in_client):
        resp = logged_in_client.post("/settings", data={
            "password": "newpw", "password2": "newpw"})
        assert resp.status_code == 200
        assert credentials_store.verify_login("admin", "newpw")
        assert not credentials_store.verify_login("admin", "password")

    def test_mismatched_password_verify_is_ignored(self, logged_in_client):
        logged_in_client.post("/settings", data={
            "password": "newpw", "password2": "different"})
        assert credentials_store.verify_login("admin", "password")

    def test_change_username_via_settings(self, logged_in_client):
        resp = logged_in_client.post("/settings", data={"username": "newadmin"})
        assert resp.status_code == 200
        assert credentials_store.get_username() == "newadmin"
        assert credentials_store.verify_login("newadmin", "password")

    def test_settings_page_shows_username(self, logged_in_client):
        credentials_store.set_username("renamed")
        resp = logged_in_client.get("/settings")
        assert b"renamed" in resp.data
