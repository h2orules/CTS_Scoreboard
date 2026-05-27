"""Tests for the public marketing pages (homepage, terms, privacy)."""
from __future__ import annotations

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.main import build_app


@pytest.fixture
def client():
    """Real composed app so the StaticFiles mount and Jinja templates are wired in."""
    fastapi_app, _sio, _asgi = build_app(
        redis_client=fakeredis.FakeRedis(),
        token_validator=lambda _t: (_ for _ in ()).throw(AssertionError("unused in these tests")),
    )
    return TestClient(fastapi_app)


def test_homepage_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    # Brand + tagline.
    assert "Swimming Scoreboard" in body
    assert "Live scoreboards for swim meets" in body
    # Footer + nav links.
    assert "info@aquagnomeapps.com" in body
    assert "https://github.com/h2orules/CTS_Scoreboard" in body
    assert 'href="/terms"' in body
    assert 'href="/privacy"' in body
    # Preview hooks: inline scoreboard SVG and the QR overlay image.
    assert "Mixed 8 &amp; Under 25 Yard Freestyle" in body
    assert "/static/img/qr-demo.svg" in body


def test_terms_page_renders(client):
    r = client.get("/terms")
    assert r.status_code == 200
    body = r.text
    assert "Terms of Use" in body
    # Boilerplate disclaimer.
    assert "AS IS" in body or "as is" in body
    # Footer still present.
    assert "info@aquagnomeapps.com" in body
    assert "https://github.com/h2orules/CTS_Scoreboard" in body


def test_privacy_page_renders(client):
    r = client.get("/privacy")
    assert r.status_code == 200
    body = r.text
    assert "Privacy Policy" in body
    # Footer still present.
    assert "info@aquagnomeapps.com" in body


def test_static_qr_svg_served(client):
    r = client.get("/static/img/qr-demo.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg")
    assert r.content.startswith(b"<svg")


def test_static_css_served(client):
    r = client.get("/static/css/site.css")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/css")


def test_static_traversal_blocked(client):
    # Starlette's StaticFiles returns 404 for any path that resolves outside
    # the mount directory; verify it doesn't expose source files.
    r = client.get("/static/../main.py")
    assert r.status_code in (404, 400)


def test_meet_routes_still_work(client):
    # Regression: unrelated /m/{meet_id} routes weren't affected by adding the
    # marketing router or the /static mount.
    r = client.get("/m/notarealmeetid")
    assert r.status_code == 404
    assert "No meet found" in r.text

    r = client.get("/m/short")
    assert r.status_code == 400
