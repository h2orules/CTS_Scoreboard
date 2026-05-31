"""Tests for the UI style picker (Classic vs Modern)."""
import pytest

from CTS_Scoreboard import app, settings


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
            "username": settings["username"],
            "password": settings["password"],
        })
        yield c


@pytest.fixture(autouse=True)
def restore_ui_style():
    original = settings.get("ui_style", "Classic")
    yield
    settings["ui_style"] = original


class TestWebHomeRendersSelectedStyle:
    def test_classic_renders_classic_css(self, client):
        settings["ui_style"] = "Classic"
        resp = client.get("/web/home")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "scoreboard_style_web.css" in html
        assert "scoreboard_style_modern.css" not in html
        assert 'data-ui-style="Classic"' in html

    def test_modern_renders_modern_css(self, client):
        settings["ui_style"] = "Modern"
        resp = client.get("/web/home")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "scoreboard_style_modern.css" in html
        assert "scoreboard_style_web.css" not in html
        assert 'data-ui-style="Modern"' in html


class TestSettingsUiStylePost:
    def test_post_modern_persists(self, logged_in_client):
        settings["ui_style"] = "Classic"
        resp = logged_in_client.post("/settings", data={
            "ui_style_form": "1",
            "ui_style": "Modern",
        })
        assert resp.status_code in (200, 302)
        assert settings["ui_style"] == "Modern"

    def test_post_invalid_falls_back_to_classic(self, logged_in_client):
        settings["ui_style"] = "Modern"
        resp = logged_in_client.post("/settings", data={
            "ui_style_form": "1",
            "ui_style": "Neon",
        })
        assert resp.status_code in (200, 302)
        assert settings["ui_style"] == "Classic"

    def test_post_without_marker_does_not_change_style(self, logged_in_client):
        settings["ui_style"] = "Modern"
        # No ui_style_form key — handler should ignore ui_style entirely.
        resp = logged_in_client.post("/settings", data={
            "ui_style": "Classic",
        })
        assert resp.status_code in (200, 302)
        assert settings["ui_style"] == "Modern"


class TestSettingsPageRendersStyleCards:
    def test_settings_page_includes_style_picker(self, logged_in_client):
        resp = logged_in_client.get("/settings")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "section-display-style" in html
        assert 'name="ui_style"' in html
        assert 'value="Classic"' in html
        assert 'value="Modern"' in html
