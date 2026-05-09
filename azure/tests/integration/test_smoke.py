"""Integration smoke tests: build a real app against fakeredis and exercise
the HTTP surface end-to-end (FastAPI + Socket.IO mounted, watchdog + routes
wired). These are slower than unit tests but still self-contained — they
don't reach out to Azure.
"""
from __future__ import annotations

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.main import build_app
from app.state import MeetStateStore


@pytest.fixture
def app_and_store():
    fake = fakeredis.FakeRedis()
    fastapi_app, _sio, _asgi = build_app(
        redis_client=fake,
        token_validator=lambda token: {"oid": "x", "tid": "y"},
    )
    store = MeetStateStore(fake)
    with TestClient(fastapi_app) as client:
        yield client, store


def test_health_endpoints_alive(app_and_store):
    client, _ = app_and_store
    assert client.get("/healthz").text == "ok"
    rdy = client.get("/readyz").json()
    assert rdy["status"] == "ready"
    ver = client.get("/version").json()
    assert "app_version" in ver
    assert ver["protocol_version_current"] >= 1


def test_unknown_meet_returns_404(app_and_store):
    client, _ = app_and_store
    res = client.get("/m/abc123")
    assert res.status_code == 404


def test_open_meet_renders_through_router(app_and_store):
    client, store = app_and_store
    meet_id = "abc123"
    store.open_meet(
        meet_id,
        host_team_name="Smoke Test",
        protocol_version=1,
        pi_account_id="oid",
    )
    bundle_id = "b1"
    store.put_template(
        meet_id,
        {
            "bundle_id": bundle_id,
            "template_text": "<html><body><div id='r'>hi</div></body></html>",
            "partials": {},
            "static_files": {},
        },
    )
    store.put_context(meet_id, {"meet_title": "Smoke", "num_lanes": 6})
    res = client.get(f"/m/{meet_id}")
    assert res.status_code == 200
    assert "id='r'" in res.text or 'id="r"' in res.text
