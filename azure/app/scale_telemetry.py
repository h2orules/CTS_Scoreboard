"""Scale-monitoring telemetry: cache size gauges + Redis INFO poller.

These are observable-gauge sources rather than hot-path counters, so they
live outside :mod:`app.telemetry` (which owns the counters/histograms used
by the request and event handlers).

Two pieces:

- **Cache size gauges**: read ``len(_TTLCache._data)`` for each per-replica
  cache on each metric collection cycle. Tells us whether the fragment
  cache's ``max_entries`` bound is being hit (FIFO eviction = signal to
  raise the cap or shorten TTL).
- **Redis INFO poller**: a background asyncio task that calls
  ``INFO memory`` + ``INFO stats`` + ``INFO clients`` every
  ``poll_interval_s`` seconds, parses the response, and stores a snapshot
  that observable-gauge callbacks read on each collection.

Both register sync callbacks with the OTel meter so they only run when
OTel is actually configured (i.e. when an Application Insights connection
string is set). In tests / local dev they are inert.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

from app.state import MeetStateStore

log = logging.getLogger(__name__)


@dataclass
class _RedisInfoSnapshot:
    """Most-recent values pulled from Redis ``INFO``.

    All floats so OTel ``Observation`` is happy. ``last_update_s`` is the
    monotonic time of the last successful poll; observable callbacks skip
    emitting if the snapshot has never been populated.
    """

    used_memory_bytes: float = 0.0
    used_memory_peak_bytes: float = 0.0
    used_memory_rss_bytes: float = 0.0
    maxmemory_bytes: float = 0.0
    evicted_keys: float = 0.0
    expired_keys: float = 0.0
    connected_clients: float = 0.0
    blocked_clients: float = 0.0
    instantaneous_ops_per_sec: float = 0.0
    pubsub_channels: float = 0.0
    pubsub_patterns: float = 0.0
    last_update_s: float = 0.0


_SNAPSHOT_FIELDS = (
    ("used_memory", "used_memory_bytes"),
    ("used_memory_peak", "used_memory_peak_bytes"),
    ("used_memory_rss", "used_memory_rss_bytes"),
    ("maxmemory", "maxmemory_bytes"),
    ("evicted_keys", "evicted_keys"),
    ("expired_keys", "expired_keys"),
    ("connected_clients", "connected_clients"),
    ("blocked_clients", "blocked_clients"),
    ("instantaneous_ops_per_sec", "instantaneous_ops_per_sec"),
    ("pubsub_channels", "pubsub_channels"),
    ("pubsub_patterns", "pubsub_patterns"),
)


@dataclass
class ScaleTelemetry:
    """Wires cache + Redis snapshot gauges into the active OTel meter and
    owns the lifecycle of the Redis INFO polling task."""

    store: MeetStateStore
    redis: Any
    poll_interval_s: float = 30.0
    snapshot: _RedisInfoSnapshot = field(default_factory=_RedisInfoSnapshot)
    _task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Register OTel gauges and start the Redis INFO polling task.

        Safe to call when OTel isn't configured: the gauge registration
        is skipped, but the poller still runs so ``snapshot`` is filled
        for any in-process inspection (and so failures in the INFO call
        get logged early).
        """
        self._register_observable_gauges()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None

    # ---- internals ----

    def _register_observable_gauges(self) -> None:
        try:
            from opentelemetry import metrics as ot_metrics
            from opentelemetry.metrics import CallbackOptions, Observation
        except Exception:  # pragma: no cover - OTel not installed
            return

        meter = ot_metrics.get_meter("cts-scoreboard.relay.scale")
        store = self.store
        snap = self.snapshot

        def cache_size_cb(
            _options: CallbackOptions,
        ) -> list[Observation]:
            return [
                Observation(
                    float(len(store._fragment_cache._data)),
                    {"cache": "fragment"},
                ),
                Observation(
                    float(len(store._current_template_cache._data)),
                    {"cache": "current_template"},
                ),
                Observation(
                    float(len(store._template_blob_cache._data)),
                    {"cache": "template_blob"},
                ),
            ]

        def cache_max_cb(
            _options: CallbackOptions,
        ) -> list[Observation]:
            return [
                Observation(
                    float(store._fragment_cache._max),
                    {"cache": "fragment"},
                ),
                Observation(
                    float(store._current_template_cache._max),
                    {"cache": "current_template"},
                ),
                Observation(
                    float(store._template_blob_cache._max),
                    {"cache": "template_blob"},
                ),
            ]

        def redis_info_cb(
            _options: CallbackOptions,
        ) -> list[Observation]:
            if snap.last_update_s == 0.0:
                return []
            return [
                Observation(snap.used_memory_bytes, {"kind": "used"}),
                Observation(snap.used_memory_peak_bytes, {"kind": "peak"}),
                Observation(snap.used_memory_rss_bytes, {"kind": "rss"}),
                Observation(snap.maxmemory_bytes, {"kind": "max"}),
            ]

        def redis_clients_cb(
            _options: CallbackOptions,
        ) -> list[Observation]:
            if snap.last_update_s == 0.0:
                return []
            return [
                Observation(snap.connected_clients, {"state": "connected"}),
                Observation(snap.blocked_clients, {"state": "blocked"}),
            ]

        def redis_ops_cb(
            _options: CallbackOptions,
        ) -> list[Observation]:
            if snap.last_update_s == 0.0:
                return []
            return [Observation(snap.instantaneous_ops_per_sec, {})]

        def redis_evictions_cb(
            _options: CallbackOptions,
        ) -> list[Observation]:
            if snap.last_update_s == 0.0:
                return []
            return [
                Observation(snap.evicted_keys, {"kind": "evicted"}),
                Observation(snap.expired_keys, {"kind": "expired"}),
            ]

        def redis_pubsub_cb(
            _options: CallbackOptions,
        ) -> list[Observation]:
            if snap.last_update_s == 0.0:
                return []
            return [
                Observation(snap.pubsub_channels, {"kind": "channels"}),
                Observation(snap.pubsub_patterns, {"kind": "patterns"}),
            ]

        meter.create_observable_gauge(
            "cache_size_entries",
            callbacks=[cache_size_cb],
            description="Current entry count per per-replica cache.",
        )
        meter.create_observable_gauge(
            "cache_max_entries",
            callbacks=[cache_max_cb],
            description="Configured max_entries per per-replica cache.",
        )
        meter.create_observable_gauge(
            "redis_memory_bytes",
            callbacks=[redis_info_cb],
            unit="By",
        )
        meter.create_observable_gauge(
            "redis_clients",
            callbacks=[redis_clients_cb],
        )
        meter.create_observable_gauge(
            "redis_instantaneous_ops_per_sec",
            callbacks=[redis_ops_cb],
        )
        meter.create_observable_gauge(
            "redis_keys_lifecycle_total",
            callbacks=[redis_evictions_cb],
            description=(
                "Cumulative server-side key disposal counts. Source: "
                "INFO stats { evicted_keys, expired_keys }."
            ),
        )
        meter.create_observable_gauge(
            "redis_pubsub",
            callbacks=[redis_pubsub_cb],
        )

    async def _poll_loop(self) -> None:
        # First poll runs immediately so a freshly started replica has data
        # within seconds, then settles into the configured cadence.
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("redis INFO poll failed")
            try:
                await asyncio.sleep(self.poll_interval_s)
            except asyncio.CancelledError:
                raise

    async def _poll_once(self) -> None:
        # Single ``INFO`` (no section) returns every field we need in one
        # round-trip; parsing is cheap. Some fakeredis versions don't
        # support INFO, in which case we just skip.
        info_fn = getattr(self.redis, "info", None)
        if info_fn is None:
            return
        try:
            info = await info_fn()
        except Exception:
            log.debug("redis.info() raised; snapshot left stale", exc_info=True)
            return
        if not isinstance(info, dict):
            return
        snap = self.snapshot
        for src, dst in _SNAPSHOT_FIELDS:
            val = info.get(src)
            if val is None:
                continue
            try:
                setattr(snap, dst, float(val))
            except (TypeError, ValueError):
                continue
        snap.last_update_s = asyncio.get_event_loop().time()
