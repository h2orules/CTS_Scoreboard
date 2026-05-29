"""Tests for MeetWatchdog."""
from __future__ import annotations

import fakeredis.aioredis
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

    store = MeetStateStore(fakeredis.aioredis.FakeRedis(), clock=now)
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


async def test_iter_active_meet_ids_yields_open_meets():
    store = MeetStateStore(fakeredis.aioredis.FakeRedis())
    await store.open_meet("a" * 15, host_team_name="A", protocol_version=1, pi_account_id="o")
    await store.open_meet("b" * 15, host_team_name="B", protocol_version=1, pi_account_id="o")
    ids = sorted([m async for m in store.iter_active_meet_ids()])
    assert ids == sorted(["a" * 15, "b" * 15])


async def test_tick_does_nothing_when_heartbeat_fresh(setup):
    store, wd, emitter, _ = setup
    await store.open_meet("m" * 15, host_team_name="A", protocol_version=1, pi_account_id="o")
    await wd.tick()
    assert emitter.calls == []
    assert (await store.get_metadata("m" * 15))["status"] == "live"


async def test_tick_marks_degraded_after_threshold(setup):
    store, wd, emitter, fake_clock = setup
    await store.open_meet("m" * 15, host_team_name="A", protocol_version=1, pi_account_id="o")
    fake_clock[0] += 65  # past degraded_after_s
    await wd.tick()
    meta = await store.get_metadata("m" * 15)
    assert meta["status"] == "degraded"
    feeds = [c for c in emitter.calls if c["event"] == "feed_status"]
    assert feeds and feeds[-1]["data"] == {"status": "degraded"}


async def test_tick_does_not_emit_degraded_twice(setup):
    store, wd, emitter, fake_clock = setup
    await store.open_meet("m" * 15, host_team_name="A", protocol_version=1, pi_account_id="o")
    fake_clock[0] += 65
    await wd.tick()
    await wd.tick()
    feeds = [c for c in emitter.calls if c["event"] == "feed_status"]
    assert len(feeds) == 1


async def test_tick_closes_meet_after_close_threshold(setup):
    store, wd, emitter, fake_clock = setup
    await store.open_meet("m" * 15, host_team_name="A", protocol_version=1, pi_account_id="o")
    fake_clock[0] += 400  # past close_after_s (300)
    await wd.tick()
    assert (await store.get_metadata("m" * 15))["status"] == "closed"
    closed = [c for c in emitter.calls if c["event"] == "meet_closed"]
    assert closed and closed[-1]["data"]["reason"] == "heartbeat_timeout"


async def test_tick_skips_already_closed_meets(setup):
    store, wd, emitter, fake_clock = setup
    await store.open_meet("m" * 15, host_team_name="A", protocol_version=1, pi_account_id="o")
    await store.close_meet("m" * 15)
    fake_clock[0] += 400
    await wd.tick()
    closed = [c for c in emitter.calls if c["event"] == "meet_closed"]
    assert closed == []


async def test_tick_handles_multiple_meets_independently(setup):
    store, _wd, _emitter, fake_clock = setup
    await store.open_meet("a" * 15, host_team_name="A", protocol_version=1, pi_account_id="oa")
    await store.open_meet("b" * 15, host_team_name="B", protocol_version=1, pi_account_id="ob")
    # Refresh "b" so its heartbeat stays fresh past the bump.
    fake_clock[0] += 65
    await store.heartbeat("b" * 15)
    await _wd.tick()
    assert (await store.get_metadata("a" * 15))["status"] == "degraded"
    assert (await store.get_metadata("b" * 15))["status"] == "live"
