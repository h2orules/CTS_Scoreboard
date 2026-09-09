"""Opt-in contract against a real Redis service (no FLUSHDB or shared test keys).

Set REDIS_TEST_URL to run against local Redis, the legacy cache, or Managed Redis.
The ordinary suite skips these tests and needs no Azure credentials.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from uuid import uuid4

import pytest
import socketio

from app.main import _build_state_redis
from app.scale_telemetry import ScaleTelemetry
from app.state import MeetKeys, MeetStateStore

pytestmark = pytest.mark.skipif(not os.environ.get("REDIS_TEST_URL"), reason="REDIS_TEST_URL not set")


async def test_live_state_and_socketio_contract():
    url = os.environ["REDIS_TEST_URL"]
    writer = _build_state_redis(url)
    reader = _build_state_redis(url)
    identifier = f"migration99-{uuid4().hex}"
    keys = MeetKeys(identifier)
    store = MeetStateStore(writer)
    remote = MeetStateStore(reader)
    channel = f"socketio-{identifier}"
    manager = socketio.AsyncRedisManager(url, channel=channel, write_only=True)
    manager._redis_connect()
    try:
        assert await writer.ping()
        await store.open_meet(
            identifier, host_team_name="Redis contract", protocol_version=1, pi_account_id=identifier,
        )
        assert await remote.get_pi_meet_id(identifier) == identifier
        assert await remote.is_meet_id_taken(identifier, by_account_id=identifier) == "self"
        await store.put_state(identifier, {"lane": 1})
        await store.put_state(identifier, {"time": "23.45"})
        assert await remote.get_state(identifier) == {"lane": 1, "time": "23.45"}
        assert 0 < await reader.ttl(keys.state) <= 86400
        await store.put_fragment(identifier, "event", "v1", "<b>Event</b>")
        assert await remote.get_fragment(identifier, "event", "v1") == "<b>Event</b>"
        bundle = {"bundle_id": "v1", "template_text": "<html>Test</html>", "static_files": {}}
        await store.put_template(identifier, bundle)
        assert await remote.get_current_template(identifier) == bundle
        await store.put_context(identifier, {"num_lanes": 6})
        assert await remote.get_context(identifier) == {"num_lanes": 6}
        assert identifier in [mid async for mid in remote.iter_active_meet_ids()]
        telemetry = ScaleTelemetry(store=store, redis=reader)
        await telemetry._poll_once()
        assert telemetry.snapshot.used_memory_bytes > 0
        assert telemetry.snapshot.connected_clients > 0

        receiver = socketio.AsyncRedisManager(url, channel=channel)
        stream = receiver._listen()
        incoming = asyncio.create_task(anext(stream))
        try:
            async with asyncio.timeout(10):
                while (await writer.pubsub_numsub(channel))[0][1] != 1:
                    await asyncio.sleep(0.05)
                await manager.emit("migration_contract", {"lane": 1}, namespace="/scoreboard", room=identifier)
                payload = json.loads(await incoming)
                assert payload["event"] == "migration_contract"
                assert payload["data"] == [{"lane": 1}]
                assert payload["room"] == identifier
        finally:
            incoming.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await incoming
            await stream.aclose()
            if receiver.pubsub is not None:
                await receiver.pubsub.aclose()
            if receiver.redis is not None:
                await receiver.redis.aclose()

        await store.close_meet(identifier)
        assert await remote.get_state(identifier) is None
        assert (await remote.get_metadata(identifier))["status"] == "closed"
    finally:
        own_keys = [key async for key in writer.scan_iter(match=f"meet:{identifier}:*")]
        if own_keys:
            await writer.delete(*own_keys)
        await writer.delete(f"pi:{identifier}:meet_id")
        await manager.pubsub.aclose()
        await manager.redis.aclose()
        await writer.aclose()
        await reader.aclose()
