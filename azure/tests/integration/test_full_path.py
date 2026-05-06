"""Cross-cutting integration: drive Pi handlers, Watchdog, and Routes together
against a single fakeredis to verify the full data path lights up end-to-end
in-process.
"""
from __future__ import annotations

from typing import Any

import fakeredis
import pytest
import socketio
from fastapi.testclient import TestClient

from app.auth import PiIdentity
from app.handlers import register_handlers
from app.main import build_app
from app.state import MeetStateStore
from app.telemetry import get_metrics, reset_for_tests
from app.watchdog import MeetWatchdog

MEET = "abc123XYZ7890ab"


def _identity(_t: str) -> PiIdentity:
    return PiIdentity(account_id="oid", tenant_id="tid", upn="pi@x.test")


def _spy_sio():
    sio = socketio.AsyncServer(async_mode="asgi")
    sessions: dict[tuple[str, str], dict[str, Any]] = {}
    emits: list[dict[str, Any]] = []

    async def save_session(sid, data, namespace="/"):
        sessions[(sid, namespace)] = dict(data)

    async def get_session(sid, namespace="/"):
        return dict(sessions.get((sid, namespace), {}))

    async def enter_room(sid, room, namespace="/"):
        return None

    async def emit(event, data=None, *, room=None, to=None, namespace="/", **_):
        emits.append({"event": event, "data": data, "room": room, "namespace": namespace})

    sio.save_session = save_session  # type: ignore[assignment]
    sio.get_session = get_session  # type: ignore[assignment]
    sio.enter_room = enter_room  # type: ignore[assignment]
    sio.emit = emit  # type: ignore[assignment]
    return sio, emits


@pytest.mark.asyncio
async def test_pi_open_meet_then_browser_renders():
    """Pi opens a meet + pushes template/state, watchdog runs a tick,
    browser GET /m/<id> renders the meet."""
    reset_for_tests()
    fake = fakeredis.FakeRedis()
    store = MeetStateStore(fake)

    # 1. Pi side: register handlers, simulate connect + meet_open + template push.
    sio, emits = _spy_sio()
    register_handlers(sio, store=store, tenant_id="tid", audience="api://aud",
                      token_validator=_identity)

    def h(ns, event):
        return sio.handlers[ns][event]

    await h("/pi", "connect")(
        "sidPi", {}, {"access_token": "ok", "meet_id": MEET, "protocol_version": 1}
    )
    await h("/pi", "meet_open")(
        "sidPi", {"meet_id": MEET, "host_team_name": "Team Foo", "protocol_version": 1}
    )
    await h("/pi", "template_push")(
        "sidPi",
        {
            "meet_id": MEET,
            "bundle_id": "bnd",
            "template_text": "<html><body><b id='live'>scoreboard</b></body></html>",
            "partials": {},
            "static_files": {},
        },
    )
    await h("/pi", "meet_context")(
        "sidPi", {"meet_title": "Foo", "num_lanes": 6}
    )

    # 2. Browser side: build a real app pinned to the same fakeredis and GET.
    fastapi_app, _sio2, _asgi = build_app(redis_client=fake, token_validator=_identity)
    with TestClient(fastapi_app) as client:
        res = client.get(f"/m/{MEET}")
    assert res.status_code == 200
    assert "id='live'" in res.text or 'id="live"' in res.text

    # 3. Watchdog: simulate heartbeat ageing past the close threshold and
    #    confirm the meet flips to closed. Watchdog clock starts "now-ish"
    #    (using a real timestamp so it's >= the heartbeat the store wrote).
    import time
    clock = [time.time() + 100.0]

    def get_clock():
        return clock[0]

    async def fake_emitter(*_a, **_kw):
        return None

    wd = MeetWatchdog(
        store=store,
        emitter=fake_emitter,
        degraded_after_s=5,
        close_after_s=10,
        clock=get_clock,
    )
    # Push heartbeat into the past so age >= close_after_s.
    store.heartbeat(MEET)
    clock[0] += 60.0
    await wd.tick()
    assert store.get_metadata(MEET).get("status") == "closed"

    # 4. Telemetry: meet_opened was recorded (stub mode) and at least one
    #    meet_closed counter increment happened.
    metrics = get_metrics()
    assert metrics.meet_opened.total >= 1
    assert metrics.meet_closed.total >= 1

    # The route now serves the friendly closed page.
    fastapi_app2, *_ = build_app(redis_client=fake, token_validator=_identity)
    with TestClient(fastapi_app2) as client:
        res = client.get(f"/m/{MEET}")
    assert res.status_code == 200
    assert "Meet closed" in res.text
    reset_for_tests()
    # Make sure spies didn't error.
    assert isinstance(emits, list)
