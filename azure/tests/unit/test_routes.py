"""Tests for the browser-facing /m/{meet_id} route and asset endpoint."""
from __future__ import annotations

import base64

import fakeredis.aioredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes import build_router, render_meet_page
from app.state import MeetStateStore

MEET = "abc123XYZ7890ab"


@pytest.fixture
def store():
    return MeetStateStore(fakeredis.aioredis.FakeRedis())


@pytest.fixture
def app_with_store(store):
    app = FastAPI()
    app.include_router(build_router(store=store))
    return app, store


@pytest.fixture
def client(app_with_store):
    app, _ = app_with_store
    return TestClient(app)


async def _seed_live_meet(store, *, meet_id=MEET, with_template=True, with_context=True):
    await store.open_meet(meet_id, host_team_name="HostU", protocol_version=1, pi_account_id="oid")
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
        await store.put_template(meet_id, bundle)
    if with_context:
        await store.put_context(meet_id, {"meet_title": "My Meet & Co"})


def test_unknown_meet_returns_404(client):
    r = client.get("/m/notarealmeetid")
    assert r.status_code == 404
    assert "No meet found" in r.text


def test_invalid_meet_id_format(client):
    r = client.get("/m/has spaces")
    assert r.status_code == 400


def test_too_short_meet_id_format(client):
    r = client.get("/m/short")
    assert r.status_code == 400


async def test_live_meet_renders_template(client, app_with_store):
    _, store = app_with_store
    await _seed_live_meet(store)
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


async def test_closed_meet_returns_closed_page(client, app_with_store):
    _, store = app_with_store
    await _seed_live_meet(store)
    await store.close_meet(MEET)
    r = client.get(f"/m/{MEET}")
    assert r.status_code == 200
    assert "No meet in session" in r.text
    assert "HostU" in r.text


async def test_expired_id_rotated_returns_410(client, app_with_store):
    _, store = app_with_store
    await _seed_live_meet(store)
    await store.mark_status(MEET, "expired_id_rotated")
    r = client.get(f"/m/{MEET}")
    assert r.status_code == 410
    assert "Link expired" in r.text


async def test_meet_starting_up_when_no_template(client, app_with_store):
    _, store = app_with_store
    await _seed_live_meet(store, with_template=False)
    r = client.get(f"/m/{MEET}")
    assert r.status_code == 503
    assert "Connecting" in r.text


async def test_meet_starting_up_when_no_context(client, app_with_store):
    _, store = app_with_store
    await _seed_live_meet(store, with_context=False)
    r = client.get(f"/m/{MEET}")
    assert r.status_code == 503


async def test_static_asset_served_with_immutable_cache(client, app_with_store):
    _, store = app_with_store
    await _seed_live_meet(store)
    r = client.get(f"/m/{MEET}/static/bid1/css/x.css")
    assert r.status_code == 200
    assert r.content == b"body{color:red}"
    assert r.headers["content-type"].startswith("text/css")
    assert "immutable" in r.headers["cache-control"]


async def test_static_asset_unknown_path(client, app_with_store):
    _, store = app_with_store
    await _seed_live_meet(store)
    r = client.get(f"/m/{MEET}/static/bid1/nope.css")
    assert r.status_code == 404


async def test_static_asset_unknown_bundle(client, app_with_store):
    _, store = app_with_store
    await _seed_live_meet(store)
    r = client.get(f"/m/{MEET}/static/wrongbid/css/x.css")
    assert r.status_code == 404


async def test_static_asset_rejects_path_traversal(client, app_with_store):
    _, store = app_with_store
    await _seed_live_meet(store)
    r = client.get(f"/m/{MEET}/static/bid1/../../etc/passwd")
    assert r.status_code in (400, 404)


async def test_old_bundle_id_still_serves_assets_after_template_rotation(client, app_with_store):
    """Browsers caching an old page must still load their pinned assets."""
    _, store = app_with_store
    await _seed_live_meet(store)
    # Roll a new bundle.
    new_bundle = {
        "bundle_id": "bid2",
        "template_path": "web/home.html",
        "template_text": "<html>v2</html>",
        "static_files": {"css/x.css": base64.b64encode(b"body{color:blue}").decode("ascii")},
        "partial_files": {},
    }
    await store.put_template(MEET, new_bundle)
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


# --- friendly meet ID validation + availability endpoint -----------------

@pytest.mark.parametrize("good", [
    "Midlakes-2026",
    "MidlakesM-26",
    "abc123XYZ7890ab",
    "Foo_bar-baz12",
    "a" * 10,
    "z" * 20,
])
async def test_meet_page_accepts_valid_friendly_id(client, app_with_store, good):
    _, store = app_with_store
    await _seed_live_meet(store, meet_id=good)
    r = client.get(f"/m/{good}")
    assert r.status_code == 200


@pytest.mark.parametrize("bad", [
    "ab",                # too short
    "a" * 9,             # too short
    "a" * 21,            # too long
    "with$dollar1",      # bad char
    "with.dot1234",      # bad char
])
def test_meet_page_rejects_invalid_friendly_id(client, bad):
    r = client.get(f"/m/{bad}")
    assert r.status_code == 400


def _identity_validator(token: str):
    from app.auth import InvalidPiTokenError, PiIdentity
    if token == "good-oid1":
        return PiIdentity(account_id="oid-1", upn="pi1@example.com", tenant_id="tid")
    if token == "good-oid2":
        return PiIdentity(account_id="oid-2", upn="pi2@example.com", tenant_id="tid")
    raise InvalidPiTokenError("bad token")


@pytest.fixture
def client_with_validator(store):
    app = FastAPI()
    app.include_router(build_router(store=store, token_validator=_identity_validator))
    return TestClient(app), store


def test_availability_no_token_returns_401(client_with_validator):
    client, _ = client_with_validator
    r = client.get("/internal/meet_id/Midlakes-2026/availability")
    assert r.status_code == 401


def test_availability_bad_token_returns_401(client_with_validator):
    client, _ = client_with_validator
    r = client.get(
        "/internal/meet_id/Midlakes-2026/availability",
        headers={"Authorization": "Bearer bogus"},
    )
    assert r.status_code == 401


def test_availability_invalid_name_returns_400(client_with_validator):
    client, _ = client_with_validator
    r = client.get(
        "/internal/meet_id/short/availability",
        headers={"Authorization": "Bearer good-oid1"},
    )
    assert r.status_code == 400


def test_availability_free_name_returns_available(client_with_validator):
    client, _ = client_with_validator
    r = client.get(
        "/internal/meet_id/Midlakes-2026/availability",
        headers={"Authorization": "Bearer good-oid1"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data == {"available": True, "owner": None}


async def test_availability_self_owned_is_available(client_with_validator):
    client, store = client_with_validator
    name = "Midlakes-2026"
    await store.open_meet(name, host_team_name="A", protocol_version=1, pi_account_id="oid-1")
    r = client.get(
        f"/internal/meet_id/{name}/availability",
        headers={"Authorization": "Bearer good-oid1"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data == {"available": True, "owner": "self"}


async def test_availability_other_owned_is_unavailable(client_with_validator):
    client, store = client_with_validator
    name = "Midlakes-2026"
    await store.open_meet(name, host_team_name="A", protocol_version=1, pi_account_id="oid-1")
    r = client.get(
        f"/internal/meet_id/{name}/availability",
        headers={"Authorization": "Bearer good-oid2"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data == {"available": False, "owner": "other"}


async def test_availability_rotated_id_is_self_for_original_owner(client_with_validator):
    # The original owner can still reclaim a rotated name until TTL elapses.
    client, store = client_with_validator
    name = "Midlakes-2026"
    await store.open_meet(name, host_team_name="A", protocol_version=1, pi_account_id="oid-1")
    await store.mark_status(name, "expired_id_rotated")
    r = client.get(
        f"/internal/meet_id/{name}/availability",
        headers={"Authorization": "Bearer good-oid1"},
    )
    assert r.status_code == 200
    assert r.json() == {"available": True, "owner": "self"}


async def test_availability_rotated_id_is_other_for_different_user(client_with_validator):
    # A different user must not be able to claim a name the original owner
    # rotated away from.
    client, store = client_with_validator
    name = "Midlakes-2026"
    await store.open_meet(name, host_team_name="A", protocol_version=1, pi_account_id="oid-1")
    await store.mark_status(name, "expired_id_rotated")
    r = client.get(
        f"/internal/meet_id/{name}/availability",
        headers={"Authorization": "Bearer good-oid2"},
    )
    assert r.status_code == 200
    assert r.json() == {"available": False, "owner": "other"}


def test_availability_disabled_when_no_validator(client):
    # The default fixture builds the app without a token validator.
    r = client.get(
        "/internal/meet_id/Midlakes-2026/availability",
        headers={"Authorization": "Bearer any"},
    )
    assert r.status_code == 503
