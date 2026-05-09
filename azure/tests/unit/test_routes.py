"""Tests for the browser-facing /m/{meet_id} route and asset endpoint."""
from __future__ import annotations

import base64

import fakeredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes import build_router, render_meet_page
from app.state import MeetStateStore

MEET = "abc123XYZ7890ab"


@pytest.fixture
def store():
    return MeetStateStore(fakeredis.FakeRedis())


@pytest.fixture
def app_with_store(store):
    app = FastAPI()
    app.include_router(build_router(store=store))
    return app, store


@pytest.fixture
def client(app_with_store):
    app, _ = app_with_store
    return TestClient(app)


def _seed_live_meet(store, *, meet_id=MEET, with_template=True, with_context=True):
    store.open_meet(meet_id, host_team_name="HostU", protocol_version=1, pi_account_id="oid")
    if with_template:
        bundle = {
            "bundle_id": "bid1",
            "template_path": "web/home.html",
            "template_text": (
                "<!doctype html>"
                "<link rel=\"stylesheet\" href=\"{{url_for('static', filename='css/x.css')}}\">"
                "<title>{{ meet_title }}</title>"
                "<body>"
                "<h1>{{ meet_title }}</h1>"
                "<script>var socket = io.connect('http://' + document.domain + ':' "
                "+ location.port + '/scoreboard');</script>"
                "</body>"
            ),
            "static_files": {
                "css/x.css": base64.b64encode(b"body{color:red}").decode("ascii"),
            },
            "partial_files": {},
        }
        store.put_template(meet_id, bundle)
    if with_context:
        store.put_context(meet_id, {"meet_title": "My Meet & Co"})


def test_unknown_meet_returns_404(client):
    r = client.get("/m/notarealmeetid")
    assert r.status_code == 404
    assert "No meet found" in r.text


def test_invalid_meet_id_format(client):
    r = client.get("/m/has-dashes")
    assert r.status_code == 400


def test_live_meet_renders_template(client, app_with_store):
    _, store = app_with_store
    _seed_live_meet(store)
    r = client.get(f"/m/{MEET}")
    assert r.status_code == 200
    assert "<title>My Meet &amp; Co</title>" in r.text
    # url_for got rewritten to the per-bundle path.
    assert f"/m/{MEET}/static/bid1/css/x.css" in r.text
    # io.connect got rewritten to use auth.
    assert "io('/scoreboard'" in r.text
    assert f"meet_id: '{MEET}'" in r.text
    # Original io.connect literal is gone.
    assert "io.connect('http://'" not in r.text


def test_closed_meet_returns_closed_page(client, app_with_store):
    _, store = app_with_store
    _seed_live_meet(store)
    store.close_meet(MEET)
    r = client.get(f"/m/{MEET}")
    assert r.status_code == 200
    assert "No meet in session" in r.text
    assert "HostU" in r.text


def test_expired_id_rotated_returns_410(client, app_with_store):
    _, store = app_with_store
    _seed_live_meet(store)
    store.mark_status(MEET, "expired_id_rotated")
    r = client.get(f"/m/{MEET}")
    assert r.status_code == 410
    assert "Link expired" in r.text


def test_meet_starting_up_when_no_template(client, app_with_store):
    _, store = app_with_store
    _seed_live_meet(store, with_template=False)
    r = client.get(f"/m/{MEET}")
    assert r.status_code == 503
    assert "Connecting" in r.text


def test_meet_starting_up_when_no_context(client, app_with_store):
    _, store = app_with_store
    _seed_live_meet(store, with_context=False)
    r = client.get(f"/m/{MEET}")
    assert r.status_code == 503


def test_static_asset_served_with_immutable_cache(client, app_with_store):
    _, store = app_with_store
    _seed_live_meet(store)
    r = client.get(f"/m/{MEET}/static/bid1/css/x.css")
    assert r.status_code == 200
    assert r.content == b"body{color:red}"
    assert r.headers["content-type"].startswith("text/css")
    assert "immutable" in r.headers["cache-control"]


def test_static_asset_unknown_path(client, app_with_store):
    _, store = app_with_store
    _seed_live_meet(store)
    r = client.get(f"/m/{MEET}/static/bid1/nope.css")
    assert r.status_code == 404


def test_static_asset_unknown_bundle(client, app_with_store):
    _, store = app_with_store
    _seed_live_meet(store)
    r = client.get(f"/m/{MEET}/static/wrongbid/css/x.css")
    assert r.status_code == 404


def test_static_asset_rejects_path_traversal(client, app_with_store):
    _, store = app_with_store
    _seed_live_meet(store)
    r = client.get(f"/m/{MEET}/static/bid1/../../etc/passwd")
    assert r.status_code in (400, 404)


def test_old_bundle_id_still_serves_assets_after_template_rotation(client, app_with_store):
    """Browsers caching an old page must still load their pinned assets."""
    _, store = app_with_store
    _seed_live_meet(store)
    # Roll a new bundle.
    new_bundle = {
        "bundle_id": "bid2",
        "template_path": "web/home.html",
        "template_text": "<html>v2</html>",
        "static_files": {"css/x.css": base64.b64encode(b"body{color:blue}").decode("ascii")},
        "partial_files": {},
    }
    store.put_template(MEET, new_bundle)
    # Old asset URL still works.
    old = client.get(f"/m/{MEET}/static/bid1/css/x.css")
    assert old.status_code == 200
    assert old.content == b"body{color:red}"
    new = client.get(f"/m/{MEET}/static/bid2/css/x.css")
    assert new.status_code == 200
    assert new.content == b"body{color:blue}"


# ---- direct render tests --------------------------------------------------

def test_render_with_partials():
    bundle = {
        "bundle_id": "b",
        "template_path": "web/home.html",
        "template_text": "<x>{% include 'partials/_p.html' %}</x>",
        "static_files": {},
        "partial_files": {"partials/_p.html": "<p>{{ title }}</p>"},
    }
    html = render_meet_page(meet_id="m" * 15, bundle=bundle, context={"title": "Hello"})
    assert "<x><p>Hello</p></x>" in html


def test_render_url_for_only_supports_static():
    bundle = {
        "bundle_id": "b",
        "template_path": "web/home.html",
        "template_text": "<a href=\"{{url_for('not_static')}}\">x</a>",
        "static_files": {},
        "partial_files": {},
    }
    html = render_meet_page(meet_id="m" * 15, bundle=bundle, context={})
    # Unsupported endpoints return '#'.
    assert 'href="#"' in html
