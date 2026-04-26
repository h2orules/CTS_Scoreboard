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


class TestPublicRoutes:
    def test_site_map(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_login_page(self, client):
        resp = client.get("/login")
        assert resp.status_code == 200

    def test_overlay_1080p(self, client):
        resp = client.get("/overlay/1080p")
        assert resp.status_code == 200

    def test_web_home(self, client):
        resp = client.get("/web/home")
        assert resp.status_code == 200

    def test_web_home_prepopulated(self, client):
        """Page should contain pre-populated JS variables and content keys."""
        resp = client.get("/web/home")
        html = resp.data.decode()
        # Should have initial content keys
        assert 'qualifyingTimesKey' in html
        assert 'blankMessageKey' in html
        # Should have pre-populated race state
        assert 'raceState' in html
        # Should have the fetchFragment helper
        assert 'fetchFragment' in html


class TestProtectedRoutes:
    def test_settings_requires_login(self, client):
        resp = client.get("/settings")
        assert resp.status_code in (302, 401)

    def test_schedule_preview_requires_login(self, client):
        resp = client.get("/schedule_preview")
        assert resp.status_code in (302, 401)

    def test_settings_accessible_when_logged_in(self, logged_in_client):
        resp = logged_in_client.get("/settings")
        assert resp.status_code == 200


class TestLogin:
    def test_login_success(self, client):
        resp = client.post("/login", data={
            "username": settings["username"],
            "password": settings["password"],
        }, follow_redirects=False)
        assert resp.status_code == 302

    def test_login_failure(self, client):
        resp = client.post("/login", data={
            "username": "wrong",
            "password": "wrong",
        })
        # The 401 error handler renders login.html with login_failed=True (200 OK)
        assert resp.status_code == 200
        assert b"login" in resp.data.lower()
