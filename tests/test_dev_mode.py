"""Tests for SCOREBOARD_MODE / is_dev_mode() and the /test route gating."""
import importlib
import os

import pytest

import credentials_store
import CTS_Scoreboard
from CTS_Scoreboard import app, settings, is_dev_mode


# ---------- is_dev_mode() unit tests ----------

class TestIsDevMode:
    def test_default_is_production(self, monkeypatch):
        monkeypatch.delenv('SCOREBOARD_MODE', raising=False)
        assert is_dev_mode() is False

    def test_explicit_production(self, monkeypatch):
        monkeypatch.setenv('SCOREBOARD_MODE', 'production')
        assert is_dev_mode() is False

    @pytest.mark.parametrize('value', ['development', 'Development', 'DEVELOPMENT', 'DeVeLoPmEnT'])
    def test_development_case_insensitive(self, monkeypatch, value):
        monkeypatch.setenv('SCOREBOARD_MODE', value)
        assert is_dev_mode() is True

    def test_garbage_value_is_not_dev(self, monkeypatch):
        monkeypatch.setenv('SCOREBOARD_MODE', 'staging')
        assert is_dev_mode() is False

    def test_empty_string_is_not_dev(self, monkeypatch):
        monkeypatch.setenv('SCOREBOARD_MODE', '')
        assert is_dev_mode() is False


# ---------- /test route gating ----------

@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def logged_in_client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        c.post("/login", data={
            "username": credentials_store.DEFAULT_USERNAME,
            "password": credentials_store.DEFAULT_PASSWORD,
        })
        yield c


class TestTestRoute:
    def test_requires_login(self, client, monkeypatch):
        # Even in dev mode, must be logged in.
        monkeypatch.setenv('SCOREBOARD_MODE', 'development')
        resp = client.get("/test")
        # Flask-Login redirects to login_view (302) or returns 401.
        assert resp.status_code in (302, 401)

    def test_returns_404_in_production(self, logged_in_client, monkeypatch):
        monkeypatch.delenv('SCOREBOARD_MODE', raising=False)
        resp = logged_in_client.get("/test")
        assert resp.status_code == 404

    def test_returns_404_when_explicit_production(self, logged_in_client, monkeypatch):
        monkeypatch.setenv('SCOREBOARD_MODE', 'production')
        resp = logged_in_client.get("/test")
        assert resp.status_code == 404

    def test_returns_200_when_dev_and_logged_in(self, logged_in_client, monkeypatch):
        monkeypatch.setenv('SCOREBOARD_MODE', 'development')
        resp = logged_in_client.get("/test")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert 'Test Controls' in body
        assert 'sim_load_event' in body
