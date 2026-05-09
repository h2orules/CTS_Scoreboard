"""Tests for MeetWatchdog."""
from __future__ import annotations

import fakeredis
import pytest

from app.state import MeetStateStore
from app.watchdog import MeetWatchdog


class _FakeEmitter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, event, data=None, *, room=None, namespace=None, **_):
        self.calls.append({"event": event, "data": data, "room": room,
                           "namespace": namespace})


@pytest.fixture
def setup():
    fake_clock = [1_700_000_000.0]

    def now() -> float:
        return fake_clock[0]

    store = MeetStateStore(fakeredis.FakeRedis(), clock=now)
    emitter = _FakeEmitter()
    wd = MeetWatchdog(
        store=store,
        emitter=emitter,
        degraded_after_s=60,
        close_after_s=300,
        tick_interval_s=10,
        clock=now,
    )
    return store, wd, emitter, fake_clock


@pytest.mark.asyncio
async def test_iter_active_meet_ids_yields_open_meets():
    store = MeetStateStore(fakeredis.FakeRedis())
    store.open_meet("a" * 15, host_team_name="A", protocol_version=1, pi_account_id="o")
    store.open_meet("b" * 15, host_team_name="B", protocol_version=1, pi_account_id="o")
    ids = sorted(store.iter_active_meet_ids())
    assert ids == sorted(["a" * 15, "b" * 15])


@pytest.mark.asyncio
async def test_tick_does_nothing_when_heartbeat_fresh(setup):
    store, wd, emitter, _ = setup
    store.open_meet("m" * 15, host_team_name="A", protocol_version=1, pi_account_id="o")
    await wd.tick()
    assert emitter.calls == []
    assert store.get_metadata("m" * 15)["status"] == "live"


@pytest.mark.asyncio
async def test_tick_marks_degraded_after_threshold(setup):
    store, wd, emitter, fake_clock = setup
    store.open_meet("m" * 15, host_team_name="A", protocol_version=1, pi_account_id="o")
    fake_clock[0] += 65  # past degraded_after_s
    await wd.tick()
    meta = store.get_metadata("m" * 15)
    assert meta["status"] == "degraded"
    feeds = [c for c in emitter.calls if c["event"] == "feed_status"]
    assert feeds and feeds[-1]["data"] == {"status": "degraded"}


@pytest.mark.asyncio
async def test_tick_does_not_emit_degraded_twice(setup):
    store, wd, emitter, fake_clock = setup
    store.open_meet("m" * 15, host_team_name="A", protocol_version=1, pi_account_id="o")
    fake_clock[0] += 65
    await wd.tick()
    await wd.tick()
    feeds = [c for c in emitter.calls if c["event"] == "feed_status"]
    assert len(feeds) == 1


@pytest.mark.asyncio
async def test_tick_closes_meet_after_close_threshold(setup):
    store, wd, emitter, fake_clock = setup
    store.open_meet("m" * 15, host_team_name="A", protocol_version=1, pi_account_id="o")
    fake_clock[0] += 400  # past close_after_s (300)
    await wd.tick()
    assert store.get_metadata("m" * 15)["status"] == "closed"
    closed = [c for c in emitter.calls if c["event"] == "meet_closed"]
    assert closed and closed[-1]["data"]["reason"] == "heartbeat_timeout"


@pytest.mark.asyncio
async def test_tick_skips_already_closed_meets(setup):
    store, wd, emitter, fake_clock = setup
    store.open_meet("m" * 15, host_team_name="A", protocol_version=1, pi_account_id="o")
    store.close_meet("m" * 15)
    fake_clock[0] += 400
    await wd.tick()
    closed = [c for c in emitter.calls if c["event"] == "meet_closed"]
    assert closed == []


@pytest.mark.asyncio
async def test_tick_handles_multiple_meets_independently(setup):
    store, _wd, _emitter, fake_clock = setup
    store.open_meet("a" * 15, host_team_name="A", protocol_version=1, pi_account_id="oa")
    store.open_meet("b" * 15, host_team_name="B", protocol_version=1, pi_account_id="ob")
    # Refresh "b" so its heartbeat stays fresh past the bump.
    fake_clock[0] += 65
    store.heartbeat("b" * 15)
    await _wd.tick()
    assert store.get_metadata("a" * 15)["status"] == "degraded"
    assert store.get_metadata("b" * 15)["status"] == "live"
