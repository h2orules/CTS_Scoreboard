"""Tests for the Redis-backed MeetStateStore using fakeredis (async)."""
from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.state import MeetStateStore


@pytest.fixture
def store():
    r = fakeredis.aioredis.FakeRedis()
    return MeetStateStore(r, clock=lambda: 1700000000.0)


async def test_open_meet_writes_metadata(store):
    await store.open_meet("m" * 15, host_team_name="HostU", protocol_version=1, pi_account_id="oid-1")
    meta = await store.get_metadata("m" * 15)
    assert meta is not None
    assert meta["host_team_name"] == "HostU"
    assert meta["status"] == "live"
    assert meta["pi_account_id"] == "oid-1"
    assert meta["opened_at"] == 1700000000.0


async def test_open_meet_idempotent_keeps_opened_at(store):
    await store.open_meet("m" * 15, host_team_name="A", protocol_version=1, pi_account_id="oid")
    # Second call after a clock bump shouldn't move opened_at.
    store._clock = lambda: 1700001000.0
    await store.open_meet("m" * 15, host_team_name="A", protocol_version=1, pi_account_id="oid")
    meta = await store.get_metadata("m" * 15)
    assert meta["opened_at"] == 1700000000.0
    assert meta["last_heartbeat"] == 1700001000.0


async def test_state_merges_partial_updates(store):
    mid = "m" * 15
    await store.put_state(mid, {"clock": "00:30.50", "lanes": [1, 2, 3]})
    await store.put_state(mid, {"clock": "00:31.00", "event": "100 Free"})
    s = await store.get_state(mid)
    assert s == {"clock": "00:31.00", "lanes": [1, 2, 3], "event": "100 Free"}


async def test_put_state_recovers_from_wrongtype_legacy_key(store):
    """Existing deployments may have meet:{id}:state as a JSON string blob
    (pre-Phase-7 schema). The first HSET would fail WRONGTYPE; we recover
    by deleting the stale key and retrying."""
    mid = "m" * 15
    key = f"meet:{mid}:state"
    # Simulate a legacy string-typed key.
    await store._r.set(key, b'{"legacy": "blob"}')
    # New writer should silently recover and produce a normal hash.
    await store.put_state(mid, {"clock": "00:30.50"})
    s = await store.get_state(mid)
    assert s == {"clock": "00:30.50"}


async def test_put_state_empty_payload_is_noop(store):
    mid = "m" * 15
    await store.put_state(mid, {"clock": "00:30"})
    await store.put_state(mid, {})  # must not wipe existing state
    s = await store.get_state(mid)
    assert s == {"clock": "00:30"}


async def test_fragments_round_trip(store):
    mid = "m" * 15
    await store.put_fragment(mid, "qualifying_info", "abc123", "<div>QT</div>")
    got = await store.get_fragment(mid, "qualifying_info")
    assert got == ("abc123", "<div>QT</div>")


async def test_invalidate_fragments_removes_them(store):
    mid = "m" * 15
    await store.put_fragment(mid, "f1", "k1", "<a/>")
    await store.put_fragment(mid, "f2", "k2", "<b/>")
    removed = await store.invalidate_fragments(mid, ["f1", "f2", "missing"])
    assert removed == 2
    assert await store.get_fragment(mid, "f1") is None
    assert await store.get_fragment(mid, "f2") is None


async def test_template_storage_idempotent_on_bundle_id(store):
    mid = "m" * 15
    bundle = {"bundle_id": "bid1", "template_text": "<html></html>",
              "static_files": {}, "partial_files": {}, "template_path": "web/home.html"}
    bid = await store.put_template(mid, bundle)
    assert bid == "bid1"
    cur = await store.get_current_template(mid)
    assert cur is not None and cur["bundle_id"] == "bid1"


async def test_get_template_blob_returns_specific_bundle(store):
    mid = "m" * 15
    bundle = {"bundle_id": "bid42", "template_text": "<html></html>",
              "static_files": {}, "partial_files": {}, "template_path": "web/home.html"}
    await store.put_template(mid, bundle)
    got = await store.get_template_blob(mid, "bid42")
    assert got is not None and got["bundle_id"] == "bid42"
    assert await store.get_template_blob(mid, "missing") is None


async def test_two_meets_do_not_share_state(store):
    a, b = "a" * 15, "b" * 15
    await store.open_meet(a, host_team_name="A", protocol_version=1, pi_account_id="oid-a")
    await store.open_meet(b, host_team_name="B", protocol_version=1, pi_account_id="oid-b")
    await store.put_state(a, {"clock": "1"})
    await store.put_state(b, {"clock": "2"})
    assert await store.get_state(a) == {"clock": "1"}
    assert await store.get_state(b) == {"clock": "2"}


async def test_close_meet_drops_state_and_marks_status(store):
    mid = "m" * 15
    await store.open_meet(mid, host_team_name="A", protocol_version=1, pi_account_id="oid")
    await store.put_state(mid, {"clock": "00:30"})
    await store.close_meet(mid)
    assert await store.get_state(mid) is None
    meta = await store.get_metadata(mid)
    assert meta["status"] == "closed"
    assert "closed_at" in meta


async def test_heartbeat_recovers_from_degraded(store):
    mid = "m" * 15
    await store.open_meet(mid, host_team_name="A", protocol_version=1, pi_account_id="oid")
    await store.mark_status(mid, "degraded")
    store._clock = lambda: 1700001000.0
    await store.heartbeat(mid)
    meta = await store.get_metadata(mid)
    assert meta["status"] == "live"
    assert meta["last_heartbeat"] == 1700001000.0


async def test_get_metadata_missing_returns_none(store):
    assert await store.get_metadata("zzzzzzzzzzzzzzz") is None


async def test_get_state_missing_returns_none(store):
    assert await store.get_state("zzzzzzzzzzzzzzz") is None


async def test_put_state_records_redis_op_latency(store):
    from app.telemetry import get_metrics, reset_for_tests
    reset_for_tests()
    metrics = get_metrics()
    mid = "m" * 15
    await store.put_state(mid, {"clock": "00:30.50"})
    put_obs = [e for e in metrics.redis_op_seconds.events
               if e[1] and e[1].get("op") == "put_state"]
    assert put_obs, "expected redis_op_seconds observation for op=put_state"
    elapsed, attrs = put_obs[-1]
    assert elapsed >= 0
    assert attrs == {"op": "put_state"}
    reset_for_tests()


async def test_open_meet_records_redis_op_latency(store):
    from app.telemetry import get_metrics, reset_for_tests
    reset_for_tests()
    metrics = get_metrics()
    await store.open_meet("m" * 15, host_team_name="X", protocol_version=1, pi_account_id="oid")
    ops = [attrs.get("op") for _, attrs in metrics.redis_op_seconds.events if attrs]
    assert "open_meet" in ops
    reset_for_tests()


async def test_get_pi_meet_id_returns_bound_id(store):
    mid = "m" * 15
    await store.open_meet(mid, host_team_name="A", protocol_version=1, pi_account_id="oid-1")
    assert await store.get_pi_meet_id("oid-1") == mid
    assert await store.get_pi_meet_id("oid-other") is None
    assert await store.get_pi_meet_id("") is None


async def test_is_meet_id_taken_no_when_empty(store):
    assert await store.is_meet_id_taken("brandnewID12", by_account_id="oid-1") == "no"


async def test_is_meet_id_taken_self_when_owner_matches(store):
    mid = "MidlakesMM-26"
    await store.open_meet(mid, host_team_name="A", protocol_version=1, pi_account_id="oid-1")
    assert await store.is_meet_id_taken(mid, by_account_id="oid-1") == "self"


async def test_is_meet_id_taken_other_when_owner_differs(store):
    mid = "MidlakesMM-26"
    await store.open_meet(mid, host_team_name="A", protocol_version=1, pi_account_id="oid-1")
    assert await store.is_meet_id_taken(mid, by_account_id="oid-2") == "other"


async def test_is_meet_id_taken_self_for_expired_id_rotated_when_owner_matches(store):
    # After rotation the original owner may still reclaim their old name
    # until the metadata TTL elapses.
    mid = "MidlakesMM-26"
    await store.open_meet(mid, host_team_name="A", protocol_version=1, pi_account_id="oid-1")
    await store.mark_status(mid, "expired_id_rotated")
    assert await store.is_meet_id_taken(mid, by_account_id="oid-1") == "self"


async def test_is_meet_id_taken_other_for_expired_id_rotated_when_owner_differs(store):
    # A different Pi must NOT be able to claim a name the original owner
    # has just rotated away from — only the creator can reclaim.
    mid = "MidlakesMM-26"
    await store.open_meet(mid, host_team_name="A", protocol_version=1, pi_account_id="oid-1")
    await store.mark_status(mid, "expired_id_rotated")
    assert await store.is_meet_id_taken(mid, by_account_id="oid-2") == "other"


async def test_is_meet_id_taken_no_for_expired_id_rotated_when_orphaned(store):
    # Back-compat: a rotated record with no recorded owner is free for
    # anyone to claim (predates the ownership field).
    mid = "OrphanedRotat1"
    await store.open_meet(mid, host_team_name="A", protocol_version=1, pi_account_id="")
    await store.mark_status(mid, "expired_id_rotated")
    assert await store.is_meet_id_taken(mid, by_account_id="oid-1") == "no"


async def test_is_meet_id_taken_other_when_orphaned(store):
    # Metadata exists but has no owner recorded — fail safe (treat as taken).
    mid = "OrphanedMeet01"
    await store.open_meet(mid, host_team_name="A", protocol_version=1, pi_account_id="")
    assert await store.is_meet_id_taken(mid, by_account_id="oid-1") == "other"


async def test_get_pi_meet_id_records_redis_op_latency(store):
    from app.telemetry import get_metrics, reset_for_tests
    reset_for_tests()
    metrics = get_metrics()
    await store.get_pi_meet_id("oid-1")
    ops = [attrs.get("op") for _, attrs in metrics.redis_op_seconds.events if attrs]
    assert "get_pi_meet_id" in ops
    reset_for_tests()


async def test_is_meet_id_taken_records_redis_op_latency(store):
    from app.telemetry import get_metrics, reset_for_tests
    reset_for_tests()
    metrics = get_metrics()
    await store.is_meet_id_taken("brandnewID12", by_account_id="oid-1")
    ops = [attrs.get("op") for _, attrs in metrics.redis_op_seconds.events if attrs]
    assert "is_meet_id_taken" in ops
    reset_for_tests()
