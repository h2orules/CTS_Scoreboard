"""Unit-level tests for /pi and /scoreboard namespace handlers.

We invoke the handlers registered on a real socketio.AsyncServer but stub the
network-facing methods (save_session, get_session, enter_room, emit) to spy on
side effects. That gives us fast deterministic coverage without spinning up
an HTTP server.
"""
from __future__ import annotations

from typing import Any

import fakeredis
import pytest
import socketio

from app.auth import InvalidPiTokenError, PiIdentity
from app.handlers import register_handlers
from app.state import MeetStateStore

MEET = "abc123XYZ7890ab"


def _make_sio_with_spies():
    sio = socketio.AsyncServer(async_mode="asgi")
    sessions: dict[tuple[str, str], dict[str, Any]] = {}
    rooms: dict[tuple[str, str], set[str]] = {}
    emits: list[dict[str, Any]] = []

    async def save_session(sid, data, namespace="/"):
        sessions[(sid, namespace)] = dict(data)

    async def get_session(sid, namespace="/"):
        return dict(sessions.get((sid, namespace), {}))

    async def enter_room(sid, room, namespace="/"):
        rooms.setdefault((room, namespace), set()).add(sid)

    async def emit(event, data=None, *, room=None, to=None, namespace="/", **_):
        emits.append({"event": event, "data": data, "room": room, "to": to, "namespace": namespace})

    sio.save_session = save_session  # type: ignore[assignment]
    sio.get_session = get_session  # type: ignore[assignment]
    sio.enter_room = enter_room  # type: ignore[assignment]
    sio.emit = emit  # type: ignore[assignment]
    return sio, sessions, rooms, emits


def _store():
    return MeetStateStore(fakeredis.FakeRedis())


def _ok_validator(_token: str) -> PiIdentity:
    return PiIdentity(account_id="oid-pi", tenant_id="tid", upn="pi@example.com")


def _bad_validator(_token: str) -> PiIdentity:
    raise InvalidPiTokenError("nope")


def _handler(sio, namespace, event):
    """Look up a handler the AsyncServer registered."""
    return sio.handlers[namespace][event]


@pytest.mark.asyncio
async def test_pi_connect_rejects_missing_auth():
    sio, *_ = _make_sio_with_spies()
    register_handlers(sio, store=_store(), tenant_id="tid", audience="api://aud",
                      token_validator=_ok_validator)
    with pytest.raises(socketio.exceptions.ConnectionRefusedError):
        await _handler(sio, "/pi", "connect")("sid1", {}, None)


@pytest.mark.asyncio
async def test_pi_connect_rejects_bad_token():
    sio, *_ = _make_sio_with_spies()
    register_handlers(sio, store=_store(), tenant_id="tid", audience="api://aud",
                      token_validator=_bad_validator)
    with pytest.raises(socketio.exceptions.ConnectionRefusedError):
        await _handler(sio, "/pi", "connect")(
            "sid1", {}, {"access_token": "bad", "meet_id": MEET, "protocol_version": 1}
        )


@pytest.mark.asyncio
async def test_pi_connect_rejects_unsupported_protocol():
    sio, *_ = _make_sio_with_spies()
    register_handlers(sio, store=_store(), tenant_id="tid", audience="api://aud",
                      token_validator=_ok_validator)
    with pytest.raises(socketio.exceptions.ConnectionRefusedError):
        await _handler(sio, "/pi", "connect")(
            "sid1", {}, {"access_token": "ok", "meet_id": MEET, "protocol_version": 99}
        )


@pytest.mark.asyncio
async def test_pi_connect_accepts_valid_handshake_and_joins_room():
    sio, sessions, rooms, _ = _make_sio_with_spies()
    register_handlers(sio, store=_store(), tenant_id="tid", audience="api://aud",
                      token_validator=_ok_validator)
    await _handler(sio, "/pi", "connect")(
        "sidPi", {}, {"access_token": "ok", "meet_id": MEET, "protocol_version": 1}
    )
    assert sessions[("sidPi", "/pi")]["pi_meet_id"] == MEET
    assert "sidPi" in rooms[(f"pi:{MEET}", "/pi")]


@pytest.mark.asyncio
async def test_meet_open_writes_metadata_and_notifies_viewers():
    sio, _, _, emits = _make_sio_with_spies()
    store = _store()
    register_handlers(sio, store=store, tenant_id="tid", audience="api://aud",
                      token_validator=_ok_validator)
    await _handler(sio, "/pi", "connect")(
        "sidPi", {}, {"access_token": "ok", "meet_id": MEET, "protocol_version": 1}
    )
    res = await _handler(sio, "/pi", "meet_open")(
        "sidPi", {"meet_id": MEET, "host_team_name": "Foo", "protocol_version": 1}
    )
    assert res == {"ok": True}
    assert store.get_metadata(MEET)["host_team_name"] == "Foo"
    feed = [e for e in emits if e["event"] == "feed_status" and e["namespace"] == "/scoreboard"]
    assert feed and feed[-1]["data"] == {"status": "live"}


@pytest.mark.asyncio
async def test_update_scoreboard_writes_state_and_fanout():
    sio, _, _, emits = _make_sio_with_spies()
    store = _store()
    register_handlers(sio, store=store, tenant_id="tid", audience="api://aud",
                      token_validator=_ok_validator)
    await _handler(sio, "/pi", "connect")(
        "sidPi", {}, {"access_token": "ok", "meet_id": MEET, "protocol_version": 1}
    )
    await _handler(sio, "/pi", "update_scoreboard")("sidPi", {"clock": "00:30.50"})
    assert store.get_state(MEET) == {"clock": "00:30.50"}
    relayed = [e for e in emits
               if e["event"] == "update_scoreboard" and e["namespace"] == "/scoreboard"]
    assert relayed and relayed[-1]["room"] == MEET


@pytest.mark.asyncio
async def test_template_push_stores_bundle_and_emits_template_changed():
    sio, _, _, emits = _make_sio_with_spies()
    store = _store()
    register_handlers(sio, store=store, tenant_id="tid", audience="api://aud",
                      token_validator=_ok_validator)
    await _handler(sio, "/pi", "connect")(
        "sidPi", {}, {"access_token": "ok", "meet_id": MEET, "protocol_version": 1}
    )
    bundle = {"bundle_id": "bid1", "template_text": "<html/>",
              "static_files": {}, "partial_files": {}, "template_path": "web/home.html"}
    res = await _handler(sio, "/pi", "template_push")("sidPi", bundle)
    assert res == {"ok": True, "bundle_id": "bid1"}
    cur = store.get_current_template(MEET)
    assert cur and cur["bundle_id"] == "bid1"
    changed = [e for e in emits if e["event"] == "template_changed"]
    assert changed and changed[-1]["data"] == {"bundle_id": "bid1"}


@pytest.mark.asyncio
async def test_invalidate_removes_fragments_and_fanout():
    sio, _, _, emits = _make_sio_with_spies()
    store = _store()
    store.open_meet(MEET, host_team_name="X", protocol_version=1, pi_account_id="oid")
    store.put_fragment(MEET, "qt", "k1", "<a/>")
    register_handlers(sio, store=store, tenant_id="tid", audience="api://aud",
                      token_validator=_ok_validator)
    await _handler(sio, "/pi", "connect")(
        "sidPi", {}, {"access_token": "ok", "meet_id": MEET, "protocol_version": 1}
    )
    res = await _handler(sio, "/pi", "invalidate")("sidPi", {"fragments": ["qt"]})
    assert res["ok"] and res["removed"] == 1
    assert store.get_fragment(MEET, "qt") is None
    invalidates = [e for e in emits if e["event"] == "invalidate"]
    assert invalidates


@pytest.mark.asyncio
async def test_heartbeat_returns_active_client_count():
    sio, _, _, _ = _make_sio_with_spies()
    store = _store()
    store.open_meet(MEET, host_team_name="X", protocol_version=1, pi_account_id="oid")
    register_handlers(sio, store=store, tenant_id="tid", audience="api://aud",
                      token_validator=_ok_validator)
    await _handler(sio, "/pi", "connect")(
        "sidPi", {}, {"access_token": "ok", "meet_id": MEET, "protocol_version": 1}
    )
    res = await _handler(sio, "/pi", "heartbeat")("sidPi", {})
    assert res["ok"] is True
    assert "active_client_count" in res


@pytest.mark.asyncio
async def test_browser_connect_unknown_meet_is_refused():
    sio, *_ = _make_sio_with_spies()
    store = _store()
    register_handlers(sio, store=store, tenant_id="tid", audience="api://aud",
                      token_validator=_ok_validator)
    with pytest.raises(socketio.exceptions.ConnectionRefusedError):
        await _handler(sio, "/scoreboard", "connect")(
            "sidB", {}, {"meet_id": "z" * 15}
        )


@pytest.mark.asyncio
async def test_browser_connect_hydrates_with_state():
    sio, _, rooms, emits = _make_sio_with_spies()
    store = _store()
    store.open_meet(MEET, host_team_name="X", protocol_version=1, pi_account_id="oid")
    store.put_state(MEET, {"clock": "00:30.50"})
    register_handlers(sio, store=store, tenant_id="tid", audience="api://aud",
                      token_validator=_ok_validator)
    await _handler(sio, "/scoreboard", "connect")("sidB", {}, {"meet_id": MEET})
    assert "sidB" in rooms[(MEET, "/scoreboard")]
    hydrate = [e for e in emits
               if e["event"] == "update_scoreboard" and e["to"] == "sidB"]
    assert hydrate and hydrate[-1]["data"] == {"clock": "00:30.50"}


@pytest.mark.asyncio
async def test_pi_disconnect_marks_meet_degraded():
    sio, _, _, emits = _make_sio_with_spies()
    store = _store()
    register_handlers(sio, store=store, tenant_id="tid", audience="api://aud",
                      token_validator=_ok_validator)
    await _handler(sio, "/pi", "connect")(
        "sidPi", {}, {"access_token": "ok", "meet_id": MEET, "protocol_version": 1}
    )
    await _handler(sio, "/pi", "meet_open")(
        "sidPi", {"meet_id": MEET, "host_team_name": "Foo", "protocol_version": 1}
    )
    await _handler(sio, "/pi", "disconnect")("sidPi")
    assert store.get_metadata(MEET)["status"] == "degraded"
    degraded = [e for e in emits
                if e["event"] == "feed_status" and e["data"] == {"status": "degraded"}]
    assert degraded


@pytest.mark.asyncio
async def test_meet_context_stored_in_redis():
    sio, _, _, _ = _make_sio_with_spies()
    store = _store()
    register_handlers(sio, store=store, tenant_id="tid", audience="api://aud",
                      token_validator=_ok_validator)
    await _handler(sio, "/pi", "connect")(
        "sidPi", {}, {"access_token": "ok", "meet_id": MEET, "protocol_version": 1}
    )
    res = await _handler(sio, "/pi", "meet_context")(
        "sidPi", {"meet_title": "Foo", "num_lanes": 6}
    )
    assert res == {"ok": True}
    ctx = store.get_context(MEET)
    assert ctx == {"meet_title": "Foo", "num_lanes": 6}


@pytest.mark.asyncio
async def test_meet_open_increments_meet_opened_metric():
    from app.telemetry import get_metrics, reset_for_tests
    reset_for_tests()
    metrics = get_metrics()
    starting_total = metrics.meet_opened.total

    sio, _, _, _ = _make_sio_with_spies()
    store = _store()
    register_handlers(sio, store=store, tenant_id="tid", audience="api://aud",
                      token_validator=_ok_validator)
    await _handler(sio, "/pi", "connect")(
        "sidPi", {}, {"access_token": "ok", "meet_id": MEET, "protocol_version": 1}
    )
    await _handler(sio, "/pi", "meet_open")(
        "sidPi", {"meet_id": MEET, "host_team_name": "X", "protocol_version": 1}
    )
    assert metrics.meet_opened.total == starting_total + 1
    reset_for_tests()


@pytest.mark.asyncio
async def test_meet_open_rejected_when_id_owned_by_other_pi():
    sio, _, _, _ = _make_sio_with_spies()
    store = _store()
    # Pre-claim by a different Pi.
    store.open_meet(MEET, host_team_name="Other", protocol_version=1, pi_account_id="oid-other")
    register_handlers(sio, store=store, tenant_id="tid", audience="api://aud",
                      token_validator=_ok_validator)
    await _handler(sio, "/pi", "connect")(
        "sidPi", {}, {"access_token": "ok", "meet_id": MEET, "protocol_version": 1}
    )
    res = await _handler(sio, "/pi", "meet_open")(
        "sidPi", {"meet_id": MEET, "host_team_name": "Foo", "protocol_version": 1}
    )
    assert res == {"ok": False, "error": "meet_id_taken"}
    # Owner unchanged.
    assert store.get_metadata(MEET)["host_team_name"] == "Other"


@pytest.mark.asyncio
async def test_meet_open_marks_previous_id_expired_when_changed():
    sio, _, _, _ = _make_sio_with_spies()
    store = _store()
    OLD = "oldMeetID12345"
    NEW = "newFriendly-26"
    # Pre-bind under the validator's account_id ("oid-pi").
    store.open_meet(OLD, host_team_name="Foo", protocol_version=1, pi_account_id="oid-pi")
    register_handlers(sio, store=store, tenant_id="tid", audience="api://aud",
                      token_validator=_ok_validator)
    await _handler(sio, "/pi", "connect")(
        "sidPi", {}, {"access_token": "ok", "meet_id": NEW, "protocol_version": 1}
    )
    res = await _handler(sio, "/pi", "meet_open")(
        "sidPi", {"meet_id": NEW, "host_team_name": "Foo", "protocol_version": 1}
    )
    assert res == {"ok": True}
    # Old id marked expired_id_rotated.
    assert store.get_metadata(OLD)["status"] == "expired_id_rotated"
    # New id is live.
    assert store.get_metadata(NEW)["status"] == "live"


@pytest.mark.asyncio
async def test_pi_connect_rejects_invalid_meet_id():
    sio, _, _, _ = _make_sio_with_spies()
    store = _store()
    register_handlers(sio, store=store, tenant_id="tid", audience="api://aud",
                      token_validator=_ok_validator)
    import socketio as _sio
    with pytest.raises(_sio.exceptions.ConnectionRefusedError):
        await _handler(sio, "/pi", "connect")(
            "sidBad", {}, {"access_token": "ok", "meet_id": "bad name!", "protocol_version": 1}
        )
