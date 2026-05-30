"""Unit-level tests for /pi and /scoreboard namespace handlers.

We invoke the handlers registered on a real socketio.AsyncServer but stub the
network-facing methods (save_session, get_session, enter_room, emit) to spy on
side effects. That gives us fast deterministic coverage without spinning up
an HTTP server.
"""
from __future__ import annotations

from typing import Any

import fakeredis.aioredis
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
    return MeetStateStore(fakeredis.aioredis.FakeRedis())


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
    assert (await store.get_metadata(MEET))["host_team_name"] == "Foo"
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
    assert await store.get_state(MEET) == {"clock": "00:30.50"}
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
    cur = await store.get_current_template(MEET)
    assert cur and cur["bundle_id"] == "bid1"
    changed = [e for e in emits if e["event"] == "template_changed"]
    assert changed and changed[-1]["data"] == {"bundle_id": "bid1"}


@pytest.mark.asyncio
async def test_fragment_event_stores_keyed_html():
    sio, _, _, _ = _make_sio_with_spies()
    store = _store()
    await store.open_meet(MEET, host_team_name="X", protocol_version=1, pi_account_id="oid")
    register_handlers(sio, store=store, tenant_id="tid", audience="api://aud",
                      token_validator=_ok_validator)
    await _handler(sio, "/pi", "connect")(
        "sidPi", {}, {"access_token": "ok", "meet_id": MEET, "protocol_version": 1}
    )
    await _handler(sio, "/pi", "fragment")(
        "sidPi", {"name": "qt", "key": "abc123", "html": "<a/>"}
    )
    assert await store.get_fragment(MEET, "qt", "abc123") == "<a/>"


@pytest.mark.asyncio
async def test_heartbeat_returns_active_client_count():
    sio, _, _, _ = _make_sio_with_spies()
    store = _store()
    await store.open_meet(MEET, host_team_name="X", protocol_version=1, pi_account_id="oid")
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
    await store.open_meet(MEET, host_team_name="X", protocol_version=1, pi_account_id="oid")
    await store.put_state(MEET, {"clock": "00:30.50"})
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
    assert (await store.get_metadata(MEET))["status"] == "degraded"
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
    ctx = await store.get_context(MEET)
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
async def test_update_scoreboard_records_event_handler_latency():
    from app.telemetry import get_metrics, reset_for_tests
    reset_for_tests()
    metrics = get_metrics()

    sio, _, _, _ = _make_sio_with_spies()
    register_handlers(sio, store=_store(), tenant_id="tid", audience="api://aud",
                      token_validator=_ok_validator)
    await _handler(sio, "/pi", "connect")(
        "sidPi", {}, {"access_token": "ok", "meet_id": MEET, "protocol_version": 1}
    )
    await _handler(sio, "/pi", "update_scoreboard")("sidPi", {"clock": "00:30.50"})

    handler_obs = [
        e for e in metrics.event_handler_seconds.events
        if e[1] and e[1].get("event") == "update_scoreboard"
    ]
    assert handler_obs, "expected an event_handler_seconds observation for update_scoreboard"
    elapsed, attrs = handler_obs[-1]
    assert elapsed >= 0
    assert attrs == {"event": "update_scoreboard", "namespace": "/pi"}
    reset_for_tests()


@pytest.mark.asyncio
async def test_update_scoreboard_records_emit_fanout_latency():
    from app.telemetry import get_metrics, reset_for_tests
    reset_for_tests()
    metrics = get_metrics()

    sio, _, _, _ = _make_sio_with_spies()
    register_handlers(sio, store=_store(), tenant_id="tid", audience="api://aud",
                      token_validator=_ok_validator)
    await _handler(sio, "/pi", "connect")(
        "sidPi", {}, {"access_token": "ok", "meet_id": MEET, "protocol_version": 1}
    )
    await _handler(sio, "/pi", "update_scoreboard")("sidPi", {"clock": "00:30.50"})

    fanout_obs = [
        e for e in metrics.emit_fanout_seconds.events
        if e[1] and e[1].get("event") == "update_scoreboard"
    ]
    assert fanout_obs, "expected an emit_fanout_seconds observation for update_scoreboard"
    reset_for_tests()


@pytest.mark.asyncio
async def test_pi_connect_increments_active_sockets_and_pi_connections():
    from app.telemetry import get_metrics, reset_for_tests
    reset_for_tests()
    metrics = get_metrics()

    sio, *_ = _make_sio_with_spies()
    register_handlers(sio, store=_store(), tenant_id="tid", audience="api://aud",
                      token_validator=_ok_validator)
    await _handler(sio, "/pi", "connect")(
        "sidPi", {}, {"access_token": "ok", "meet_id": MEET, "protocol_version": 1}
    )
    pi_obs = [e for e in metrics.active_sockets.events
              if e[1] and e[1].get("namespace") == "/pi"]
    assert pi_obs and pi_obs[-1][0] == 1
    assert metrics.pi_connections.total == 1
    reset_for_tests()


@pytest.mark.asyncio
async def test_pi_disconnect_decrements_active_sockets_and_pi_connections():
    from app.telemetry import get_metrics, reset_for_tests
    reset_for_tests()
    metrics = get_metrics()

    sio, *_ = _make_sio_with_spies()
    register_handlers(sio, store=_store(), tenant_id="tid", audience="api://aud",
                      token_validator=_ok_validator)
    await _handler(sio, "/pi", "connect")(
        "sidPi", {}, {"access_token": "ok", "meet_id": MEET, "protocol_version": 1}
    )
    await _handler(sio, "/pi", "disconnect")("sidPi")
    # connect added 1, disconnect added -1 → net 0
    pi_ns_total = sum(
        delta for delta, attrs in metrics.active_sockets.events
        if attrs and attrs.get("namespace") == "/pi"
    )
    assert pi_ns_total == 0
    assert metrics.pi_connections.total == 0
    reset_for_tests()


@pytest.mark.asyncio
async def test_browser_connect_increments_active_sockets_scoreboard():
    from app.telemetry import get_metrics, reset_for_tests
    reset_for_tests()
    metrics = get_metrics()

    sio, *_ = _make_sio_with_spies()
    store = _store()
    await store.open_meet(MEET, host_team_name="X", protocol_version=1, pi_account_id="oid")
    register_handlers(sio, store=store, tenant_id="tid", audience="api://aud",
                      token_validator=_ok_validator)
    await _handler(sio, "/scoreboard", "connect")("sidB", {}, {"meet_id": MEET})
    sb_obs = [e for e in metrics.active_sockets.events
              if e[1] and e[1].get("namespace") == "/scoreboard"]
    assert sb_obs and sb_obs[-1][0] == 1
    reset_for_tests()


@pytest.mark.asyncio
async def test_browser_disconnect_decrements_active_sockets_scoreboard():
    from app.telemetry import get_metrics, reset_for_tests
    reset_for_tests()
    metrics = get_metrics()

    sio, *_ = _make_sio_with_spies()
    store = _store()
    await store.open_meet(MEET, host_team_name="X", protocol_version=1, pi_account_id="oid")
    register_handlers(sio, store=store, tenant_id="tid", audience="api://aud",
                      token_validator=_ok_validator)
    await _handler(sio, "/scoreboard", "connect")("sidB", {}, {"meet_id": MEET})
    await _handler(sio, "/scoreboard", "disconnect")("sidB")
    sb_total = sum(
        delta for delta, attrs in metrics.active_sockets.events
        if attrs and attrs.get("namespace") == "/scoreboard"
    )
    assert sb_total == 0
    reset_for_tests()


@pytest.mark.asyncio
async def test_meet_open_rejected_when_id_owned_by_other_pi():
    sio, _, _, _ = _make_sio_with_spies()
    store = _store()
    # Pre-claim by a different Pi.
    await store.open_meet(MEET, host_team_name="Other", protocol_version=1, pi_account_id="oid-other")
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
    assert (await store.get_metadata(MEET))["host_team_name"] == "Other"


@pytest.mark.asyncio
async def test_meet_open_marks_previous_id_expired_when_changed():
    sio, _, _, _ = _make_sio_with_spies()
    store = _store()
    OLD = "oldMeetID12345"
    NEW = "newFriendly-26"
    # Pre-bind under the validator's account_id ("oid-pi").
    await store.open_meet(OLD, host_team_name="Foo", protocol_version=1, pi_account_id="oid-pi")
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
    assert (await store.get_metadata(OLD))["status"] == "expired_id_rotated"
    # New id is live.
    assert (await store.get_metadata(NEW))["status"] == "live"


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


@pytest.mark.asyncio
async def test_meet_open_allows_original_owner_to_reclaim_rotated_id():
    # The original Pi may reclaim a previously-rotated name until the
    # metadata TTL elapses.
    sio, _, _, _ = _make_sio_with_spies()
    store = _store()
    MID = "Midlakes-2026a"
    # _ok_validator returns account_id="oid-pi".
    await store.open_meet(MID, host_team_name="Foo", protocol_version=1, pi_account_id="oid-pi")
    await store.mark_status(MID, "expired_id_rotated")
    register_handlers(sio, store=store, tenant_id="tid", audience="api://aud",
                      token_validator=_ok_validator)
    await _handler(sio, "/pi", "connect")(
        "sidPi", {}, {"access_token": "ok", "meet_id": MID, "protocol_version": 1}
    )
    res = await _handler(sio, "/pi", "meet_open")(
        "sidPi", {"meet_id": MID, "host_team_name": "Foo", "protocol_version": 1}
    )
    assert res == {"ok": True}
    meta = await store.get_metadata(MID)
    assert meta["status"] == "live"
    assert meta["pi_account_id"] == "oid-pi"


@pytest.mark.asyncio
async def test_meet_open_rejects_other_pi_reclaiming_rotated_id():
    # A *different* Pi must not be able to grab a name the original owner
    # has rotated away from.
    sio, _, _, _ = _make_sio_with_spies()
    store = _store()
    MID = "Midlakes-2026b"
    # Original owner is "oid-original"; the connecting Pi's validator
    # returns "oid-pi" (different identity).
    await store.open_meet(MID, host_team_name="Orig", protocol_version=1, pi_account_id="oid-original")
    await store.mark_status(MID, "expired_id_rotated")
    register_handlers(sio, store=store, tenant_id="tid", audience="api://aud",
                      token_validator=_ok_validator)
    await _handler(sio, "/pi", "connect")(
        "sidPi", {}, {"access_token": "ok", "meet_id": MID, "protocol_version": 1}
    )
    res = await _handler(sio, "/pi", "meet_open")(
        "sidPi", {"meet_id": MID, "host_team_name": "Intruder", "protocol_version": 1}
    )
    assert res == {"ok": False, "error": "meet_id_taken"}
    # Original metadata unchanged: still owned by oid-original, still rotated.
    meta = await store.get_metadata(MID)
    assert meta["pi_account_id"] == "oid-original"
    assert meta["status"] == "expired_id_rotated"
    assert meta["host_team_name"] == "Orig"


@pytest.mark.asyncio
async def test_coalescer_merges_update_scoreboard_frames():
    """Multiple update_scoreboard frames within the window collapse to a
    single HSET + single emit, with last-write-wins on overlapping fields."""
    import asyncio

    sio, _, _, emits = _make_sio_with_spies()
    store = _store()
    register_handlers(
        sio, store=store, tenant_id="tid", audience="api://aud",
        token_validator=_ok_validator, coalesce_window_s=0.05,
    )
    await _handler(sio, "/pi", "connect")(
        "sidPi", {}, {"access_token": "ok", "meet_id": MEET, "protocol_version": 1}
    )

    # Three rapid frames: lane_1 first, lane_2 added, clock overwrites.
    await _handler(sio, "/pi", "update_scoreboard")("sidPi", {"lane_1": "A", "clock": "00:01.0"})
    await _handler(sio, "/pi", "update_scoreboard")("sidPi", {"lane_2": "B", "clock": "00:02.0"})
    await _handler(sio, "/pi", "update_scoreboard")("sidPi", {"clock": "00:03.0"})

    # Nothing emitted yet — still inside the window.
    relayed = [e for e in emits if e["event"] == "update_scoreboard"
               and e["namespace"] == "/scoreboard"]
    assert relayed == []

    # Wait past the flush window.
    await asyncio.sleep(0.12)

    relayed = [e for e in emits if e["event"] == "update_scoreboard"
               and e["namespace"] == "/scoreboard"]
    assert len(relayed) == 1, f"expected one coalesced emit, got {len(relayed)}"
    assert relayed[0]["data"] == {"lane_1": "A", "lane_2": "B", "clock": "00:03.0"}
    assert await store.get_state(MEET) == {
        "lane_1": "A", "lane_2": "B", "clock": "00:03.0",
    }


@pytest.mark.asyncio
async def test_coalescer_separates_distinct_events():
    """update_scoreboard and event_info coalesce independently, each flushing
    one combined HSET+emit per window."""
    import asyncio

    sio, _, _, emits = _make_sio_with_spies()
    store = _store()
    register_handlers(
        sio, store=store, tenant_id="tid", audience="api://aud",
        token_validator=_ok_validator, coalesce_window_s=0.05,
    )
    await _handler(sio, "/pi", "connect")(
        "sidPi", {}, {"access_token": "ok", "meet_id": MEET, "protocol_version": 1}
    )

    await _handler(sio, "/pi", "update_scoreboard")("sidPi", {"clock": "00:01.0"})
    await _handler(sio, "/pi", "event_info")("sidPi", {"event": 1, "heat": 2})
    await _handler(sio, "/pi", "event_info")("sidPi", {"event": 1, "heat": 3})

    await asyncio.sleep(0.12)

    upd = [e for e in emits if e["event"] == "update_scoreboard"]
    evi = [e for e in emits if e["event"] == "event_info"]
    assert len(upd) == 1 and upd[0]["data"] == {"clock": "00:01.0"}
    assert len(evi) == 1 and evi[0]["data"] == {"event": 1, "heat": 3}
    state = await store.get_state(MEET)
    assert state["clock"] == "00:01.0"
    assert state["event_info"] == {"event": 1, "heat": 3}
