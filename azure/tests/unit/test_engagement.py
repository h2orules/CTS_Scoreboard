"""Tests for the viewer-engagement telemetry surface.

Covers:
- ``window.__ENGAGEMENT`` injection into the meet page.
- device_hash determinism + IP/UA sensitivity.
- POST /m/{meet_id}/api/telemetry validation, rate limiting, and forwarding.
- pi_local_date persistence through open_meet + heartbeat.
"""
from __future__ import annotations

import logging

import fakeredis.aioredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.routes import _compute_device_hash, _inject_engagement, build_router
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


async def _seed(store, *, pi_local_date="2026-03-28"):
    await store.open_meet(
        MEET,
        host_team_name="HostU",
        protocol_version=1,
        pi_account_id="oid",
        pi_local_date=pi_local_date,
    )
    bundle = {
        "bundle_id": "bid1",
        "template_path": "web/home.html",
        "template_text": (
            "<!doctype html><html><head><title>x</title></head>"
            "<body><script>var s = io.connect('http://' + document.domain + ':' "
            "+ location.port + '/scoreboard');</script></body></html>"
        ),
        "static_files": {},
        "partial_files": {},
    }
    await store.put_template(MEET, bundle)
    await store.put_context(MEET, {"meet_title": "T"})


# ---------------------------------------------------------------------------
# device_hash
# ---------------------------------------------------------------------------

class _FakeRequest:
    def __init__(self, ip: str, ua: str, xff: str = "") -> None:
        self.client = type("c", (), {"host": ip})()
        self.headers = {"x-forwarded-for": xff, "user-agent": ua}


def test_device_hash_stable_and_short():
    r1 = _FakeRequest("1.2.3.4", "Mozilla/5.0")
    r2 = _FakeRequest("1.2.3.4", "Mozilla/5.0")
    h1 = _compute_device_hash(r1, "salt")
    h2 = _compute_device_hash(r2, "salt")
    assert h1 == h2
    assert len(h1) == 16
    assert all(c in "0123456789abcdef" for c in h1)


def test_device_hash_changes_with_ip_or_ua_or_salt():
    base = _compute_device_hash(_FakeRequest("1.2.3.4", "ua-A"), "salt")
    assert _compute_device_hash(_FakeRequest("9.9.9.9", "ua-A"), "salt") != base
    assert _compute_device_hash(_FakeRequest("1.2.3.4", "ua-B"), "salt") != base
    assert _compute_device_hash(_FakeRequest("1.2.3.4", "ua-A"), "salt2") != base


def test_device_hash_prefers_xff():
    h_xff = _compute_device_hash(_FakeRequest("10.0.0.1", "ua", xff="5.5.5.5, 10.0.0.1"), "s")
    h_direct = _compute_device_hash(_FakeRequest("5.5.5.5", "ua"), "s")
    assert h_xff == h_direct


# ---------------------------------------------------------------------------
# injection
# ---------------------------------------------------------------------------

def test_inject_engagement_writes_bootstrap_before_head():
    html = "<html><head><title>x</title></head><body>y</body></html>"
    out = _inject_engagement(html, meet_id=MEET, pi_local_date="2026-03-28", device_hash="abc123")
    assert "window.__ENGAGEMENT=" in out
    assert MEET in out
    assert "abc123" in out
    assert "2026-03-28" in out
    assert "engagement.js" in out
    # Bootstrap must land before </head>, not after.
    assert out.index("window.__ENGAGEMENT") < out.index("</head>")


def test_inject_engagement_is_noop_without_head():
    html = "<p>no head here</p>"
    assert _inject_engagement(html, meet_id=MEET, pi_local_date="", device_hash="x") == html


async def test_meet_page_injects_engagement(client, app_with_store):
    _, store = app_with_store
    await _seed(store, pi_local_date="2026-03-28")
    r = client.get(f"/m/{MEET}")
    assert r.status_code == 200
    assert "window.__ENGAGEMENT" in r.text
    assert '"pi_local_date":"2026-03-28"' in r.text
    assert '"telemetry_endpoint":"/m/' + MEET + '/api/telemetry"' in r.text


# ---------------------------------------------------------------------------
# POST /api/telemetry
# ---------------------------------------------------------------------------

async def test_telemetry_post_accepts_batch(client, app_with_store, caplog):
    _, store = app_with_store
    await _seed(store)
    caplog.set_level(logging.INFO, logger="cts.viewer")
    payload = {
        "events": [
            {"name": "viewer_page_load", "props": {"viewer_id": "v1", "ts_ms": 1}},
            {"name": "viewer_heartbeat", "props": {"viewer_id": "v1", "tenure_ms": 30000}},
        ]
    }
    r = client.post(f"/m/{MEET}/api/telemetry", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body == {"ok": True, "accepted": 2}
    # Each event was logged through emit_viewer_event.
    names = [rec.message for rec in caplog.records if rec.name == "cts.viewer"]
    assert "viewer_page_load" in names
    assert "viewer_heartbeat" in names


async def test_telemetry_post_rejects_unknown_meet(client):
    r = client.post(f"/m/{MEET}/api/telemetry", json={"events": [{"name": "viewer_page_load"}]})
    assert r.status_code == 404


async def test_telemetry_post_rejects_bad_meet_id(client):
    r = client.post("/m/has spaces/api/telemetry", json={"events": []})
    assert r.status_code == 400


async def test_telemetry_post_rejects_empty_events(client, app_with_store):
    _, store = app_with_store
    await _seed(store)
    r = client.post(f"/m/{MEET}/api/telemetry", json={"events": []})
    assert r.status_code == 400


async def test_telemetry_post_drops_non_viewer_names(client, app_with_store, caplog):
    _, store = app_with_store
    await _seed(store)
    caplog.set_level(logging.INFO, logger="cts.viewer")
    r = client.post(f"/m/{MEET}/api/telemetry", json={
        "events": [
            {"name": "viewer_heartbeat", "props": {}},
            {"name": "evil_event", "props": {}},
            {"name": "noprefix", "props": {}},
        ]
    })
    assert r.status_code == 200
    assert r.json()["accepted"] == 1


async def test_telemetry_post_accepts_message_board_event(client, app_with_store, caplog):
    _, store = app_with_store
    await _seed(store)
    caplog.set_level(logging.INFO, logger="cts.viewer")
    r = client.post(f"/m/{MEET}/api/telemetry", json={
        "events": [{
            "name": "viewer_message_board_view",
            "props": {"viewer_id": "v1", "active": True, "page_index": 0, "tenure_ms": 1234},
        }]
    })
    assert r.status_code == 200
    assert r.json()["accepted"] == 1
    rec = next(rec for rec in caplog.records if rec.name == "cts.viewer")
    assert rec.message == "viewer_message_board_view"
    assert rec.__dict__.get("active") is True
    assert rec.__dict__.get("page_index") == 0


async def test_telemetry_post_caps_at_50_events(client, app_with_store):
    _, store = app_with_store
    await _seed(store)
    r = client.post(f"/m/{MEET}/api/telemetry", json={
        "events": [{"name": "viewer_heartbeat", "props": {}}] * 100,
    })
    assert r.status_code == 200
    assert r.json()["accepted"] == 50


async def test_telemetry_post_rate_limits(client, app_with_store, monkeypatch):
    _, store = app_with_store
    await _seed(store)
    # Drop the limit aggressively so the test stays fast.
    get_settings.cache_clear()
    monkeypatch.setenv("AZURE_TELEMETRY_RATE_LIMIT_PER_MIN", "3")
    get_settings.cache_clear()
    try:
        ok = 0
        limited = 0
        for _ in range(6):
            r = client.post(f"/m/{MEET}/api/telemetry", json={
                "events": [{"name": "viewer_heartbeat"}],
            })
            if r.status_code == 200:
                ok += 1
            elif r.status_code == 429:
                limited += 1
        assert ok == 3
        assert limited == 3
    finally:
        get_settings.cache_clear()


async def test_telemetry_post_overrides_spoofed_meet_id(client, app_with_store, caplog):
    _, store = app_with_store
    await _seed(store, pi_local_date="2026-03-28")
    caplog.set_level(logging.INFO, logger="cts.viewer")
    r = client.post(f"/m/{MEET}/api/telemetry", json={
        "events": [{
            "name": "viewer_page_load",
            "props": {"meet_id": "other-meet-id", "device_hash": "fake"},
        }]
    })
    assert r.status_code == 200
    # The logged record must carry the URL meet_id, not the spoofed one.
    rec = next(r for r in caplog.records if r.name == "cts.viewer")
    assert rec.__dict__.get("meet_id") == MEET
    assert rec.__dict__.get("device_hash") != "fake"


# ---------------------------------------------------------------------------
# pi_local_date persistence
# ---------------------------------------------------------------------------

async def test_open_meet_persists_pi_local_date(store):
    await store.open_meet(
        MEET, host_team_name="H", protocol_version=1,
        pi_account_id="oid", pi_local_date="2026-03-28",
    )
    meta = await store.get_metadata(MEET)
    assert meta is not None
    assert meta["pi_local_date"] == "2026-03-28"


async def test_heartbeat_refreshes_pi_local_date(store):
    await store.open_meet(
        MEET, host_team_name="H", protocol_version=1,
        pi_account_id="oid", pi_local_date="2026-03-28",
    )
    await store.heartbeat(MEET, pi_local_date="2026-03-29")
    meta = await store.get_metadata(MEET)
    assert meta is not None
    assert meta["pi_local_date"] == "2026-03-29"


async def test_heartbeat_preserves_pi_local_date_when_omitted(store):
    await store.open_meet(
        MEET, host_team_name="H", protocol_version=1,
        pi_account_id="oid", pi_local_date="2026-03-28",
    )
    await store.heartbeat(MEET)
    meta = await store.get_metadata(MEET)
    assert meta is not None
    assert meta["pi_local_date"] == "2026-03-28"
