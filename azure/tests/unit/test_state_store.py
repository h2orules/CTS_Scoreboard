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
    got = await store.get_fragment(mid, "qualifying_info", "abc123")
    assert got == "<div>QT</div>"


async def test_get_fragment_unknown_key_returns_none(store):
    mid = "m" * 15
    await store.put_fragment(mid, "qualifying_info", "abc123", "<div>QT</div>")
    assert await store.get_fragment(mid, "qualifying_info", "otherkey") is None


async def test_put_fragment_distinct_keys_coexist(store):
    """Two different content versions live under different Redis keys, so
    older browsers holding a stale key can still serve."""
    mid = "m" * 15
    await store.put_fragment(mid, "qt", "k1", "<a>v1</a>")
    await store.put_fragment(mid, "qt", "k2", "<a>v2</a>")
    assert await store.get_fragment(mid, "qt", "k1") == "<a>v1</a>"
    assert await store.get_fragment(mid, "qt", "k2") == "<a>v2</a>"


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


# ============================================================
# C1/C2: per-replica TTL caches
# ============================================================

async def test_get_fragment_hits_cache_within_ttl():
    """A second get_fragment within the TTL must not hit Redis."""
    r = fakeredis.aioredis.FakeRedis()
    s = MeetStateStore(r, fragment_cache_ttl=5.0)
    mid = "f" * 15
    await s.put_fragment(mid, "footer", "k1", "<p>hi</p>")
    # Tamper with Redis behind the store: if cache works, we never see this.
    await r.delete(f"meet:{mid}:fragment:footer:k1")
    got = await s.get_fragment(mid, "footer", "k1")
    assert got == "<p>hi</p>"


async def test_get_fragment_new_key_misses_old_cache_entry():
    """A different key is a different cache entry — no risk of stale 304s
    after the content-hash refactor."""
    r = fakeredis.aioredis.FakeRedis()
    s = MeetStateStore(r, fragment_cache_ttl=60.0)
    mid = "f" * 15
    await s.put_fragment(mid, "footer", "k1", "<p>a</p>")
    assert (await s.get_fragment(mid, "footer", "k1")) == "<p>a</p>"
    await s.put_fragment(mid, "footer", "k2", "<p>b</p>")
    assert (await s.get_fragment(mid, "footer", "k2")) == "<p>b</p>"
    # Both keys remain independently cacheable.
    assert (await s.get_fragment(mid, "footer", "k1")) == "<p>a</p>"


async def test_fragment_cache_disabled_with_ttl_zero():
    """ttl=0 means every read hits Redis."""
    r = fakeredis.aioredis.FakeRedis()
    s = MeetStateStore(r, fragment_cache_ttl=0.0)
    mid = "f" * 15
    await s.put_fragment(mid, "footer", "k1", "<p>hi</p>")
    # Delete behind the store; with caching disabled, the read must miss.
    await r.delete(f"meet:{mid}:fragment:footer:k1")
    assert await s.get_fragment(mid, "footer", "k1") is None


async def test_fragment_cache_evicts_oldest_at_capacity():
    """FIFO eviction when max_entries is reached."""
    r = fakeredis.aioredis.FakeRedis()
    s = MeetStateStore(r, fragment_cache_ttl=60.0, fragment_cache_max_entries=2)
    mid = "f" * 15
    await s.put_fragment(mid, "a", "k1", "A")
    await s.put_fragment(mid, "b", "k2", "B")
    await s.put_fragment(mid, "c", "k3", "C")  # evicts "a"
    # "a" is gone from cache; deleting Redis behind it proves it has to refetch.
    await r.delete(f"meet:{mid}:fragment:a:k1")
    assert await s.get_fragment(mid, "a", "k1") is None
    # "c" is fresh in cache; Redis-side delete doesn't matter.
    await r.delete(f"meet:{mid}:fragment:c:k3")
    assert await s.get_fragment(mid, "c", "k3") == "C"


async def test_template_blob_cache_immutable_per_bundle_id():
    """get_template_blob caches forever per (meet_id, bundle_id)."""
    r = fakeredis.aioredis.FakeRedis()
    s = MeetStateStore(r)
    mid = "t" * 15
    bundle = {"bundle_id": "abc1", "template_text": "hi"}
    bid = await s.put_template(mid, bundle)
    assert bid == "abc1"
    # Wipe Redis; the cached blob still answers.
    await r.delete(f"meet:{mid}:template:abc1")
    got = await s.get_template_blob(mid, "abc1")
    assert got == bundle


async def test_get_current_template_uses_cache():
    """get_current_template short-circuits within its TTL."""
    r = fakeredis.aioredis.FakeRedis()
    s = MeetStateStore(r, current_template_cache_ttl=5.0)
    mid = "t" * 15
    bundle = {"bundle_id": "abc1", "template_text": "hi"}
    await s.put_template(mid, bundle)
    assert (await s.get_current_template(mid)) == bundle
    # Wipe Redis: cache hit serves stale-but-correct data.
    await r.delete(f"meet:{mid}:current_template")
    await r.delete(f"meet:{mid}:template:abc1")
    assert (await s.get_current_template(mid)) == bundle


async def test_close_meet_clears_caches():
    """close_meet must drop every cached entry for that meet."""
    r = fakeredis.aioredis.FakeRedis()
    s = MeetStateStore(r, fragment_cache_ttl=60.0, current_template_cache_ttl=60.0)
    mid = "c" * 15
    await s.open_meet(mid, host_team_name="H", protocol_version=1, pi_account_id="oid")
    await s.put_fragment(mid, "footer", "k1", "<p/>")
    await s.put_template(mid, {"bundle_id": "b1", "template_text": "x"})
    await s.close_meet(mid)
    # Both caches must be empty for this meet.
    assert s._fragment_cache.get((mid, "footer", "k1")) is None
    assert s._current_template_cache.get(mid) is None
    assert s._template_blob_cache.get((mid, "b1")) is None


# ============================================================
# Cache hit/miss telemetry counters
# ============================================================

async def test_cache_hit_miss_counters_recorded():
    from app.telemetry import get_metrics, reset_for_tests
    reset_for_tests()
    r = fakeredis.aioredis.FakeRedis()
    s = MeetStateStore(r, fragment_cache_ttl=5.0)
    mid = "c" * 15
    await s.put_fragment(mid, "footer", "k1", "<p>hi</p>")
    # put_fragment seeds the cache, so clear it to exercise the miss path.
    s._fragment_cache.clear()
    # First read after clear: cache empty -> miss + uncached fetch fills cache.
    await s.get_fragment(mid, "footer", "k1")
    # Second read: hit.
    await s.get_fragment(mid, "footer", "k1")
    m = get_metrics()
    hit_ops = [attrs["op"] for _, attrs in m.cache_hits.events if attrs]
    miss_ops = [attrs["op"] for _, attrs in m.cache_misses.events if attrs]
    assert hit_ops.count("get_fragment") == 1
    assert miss_ops.count("get_fragment") == 1


async def test_cache_miss_not_recorded_when_cache_disabled():
    from app.telemetry import get_metrics, reset_for_tests
    reset_for_tests()
    r = fakeredis.aioredis.FakeRedis()
    s = MeetStateStore(r, fragment_cache_ttl=0.0)  # disabled
    mid = "d" * 15
    await s.put_fragment(mid, "footer", "k1", "<p>hi</p>")
    await s.get_fragment(mid, "footer", "k1")
    m = get_metrics()
    # Cache disabled: every read still goes through the "miss" path
    # (cache.get -> None) so misses ARE counted; hits stay zero because
    # the cache never serves a value.
    miss_ops = [attrs["op"] for _, attrs in m.cache_misses.events if attrs]
    hit_ops = [attrs["op"] for _, attrs in m.cache_hits.events if attrs]
    assert miss_ops.count("get_fragment") == 1
    assert hit_ops.count("get_fragment") == 0
