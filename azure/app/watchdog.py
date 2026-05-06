"""Heartbeat watchdog for live meets (Phase 6).

A long-running coroutine periodically inspects every active meet's metadata.
When ``last_heartbeat`` is older than the degraded threshold we flip the meet
into the ``degraded`` state and notify viewers; once it's older than the close
threshold we close the meet entirely (drops hot state, emits ``meet_closed``).

The watchdog is intentionally driven by ``time.time()``-derived deltas rather
than wall-clock comparisons of heartbeats, so the Pi clock and the relay clock
need only be roughly in sync.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from app.state import MeetStateStore
from app.telemetry import get_metrics

log = logging.getLogger(__name__)


class MeetWatchdog:
    """Background heartbeat scanner.

    Parameters
    ----------
    store
        The shared :class:`MeetStateStore`.
    emitter
        Async callable used to fan events out to browsers; matches the
        ``socketio.AsyncServer.emit`` signature for the keyword args we use:
        ``emitter(event, data, room=..., namespace=...)``.
    degraded_after_s
        Heartbeat age (seconds) past which a live meet flips to ``degraded``.
    close_after_s
        Heartbeat age past which a meet is closed entirely.
    tick_interval_s
        How often the run loop scans (also the worst-case detection latency).
    clock
        Override the time source for tests.
    """

    def __init__(
        self,
        *,
        store: MeetStateStore,
        emitter: Callable[..., Awaitable[None]],
        degraded_after_s: int = 60,
        close_after_s: int = 8 * 3600,
        tick_interval_s: int = 30,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self._emit = emitter
        self.degraded_after_s = degraded_after_s
        self.close_after_s = close_after_s
        self.tick_interval_s = tick_interval_s
        self._clock = clock
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def tick(self) -> None:
        """One pass over all active meets. Public for unit tests."""
        now = self._clock()
        metrics = get_metrics()
        for meet_id in list(self.store.iter_active_meet_ids()):
            meta = self.store.get_metadata(meet_id)
            if not meta:
                continue
            status = meta.get("status")
            if status in ("closed", "expired_id_rotated"):
                continue
            last_hb = float(meta.get("last_heartbeat") or 0.0)
            age = now - last_hb
            if age >= self.close_after_s:
                self.store.close_meet(meet_id)
                metrics.meet_closed.add(1, {"meet_id": meet_id, "reason": "heartbeat_timeout"})
                await self._emit(
                    "meet_closed",
                    {"meet_id": meet_id, "reason": "heartbeat_timeout"},
                    room=meet_id,
                    namespace="/scoreboard",
                )
                log.warning(
                    "watchdog: closed meet=%s after %.1fs without heartbeat",
                    meet_id, age,
                )
            elif age >= self.degraded_after_s and status != "degraded":
                self.store.mark_status(meet_id, "degraded")
                metrics.meet_degraded.add(1, {"meet_id": meet_id})
                await self._emit(
                    "feed_status",
                    {"status": "degraded"},
                    room=meet_id,
                    namespace="/scoreboard",
                )
                log.info(
                    "watchdog: meet=%s degraded after %.1fs without heartbeat",
                    meet_id, age,
                )

    async def _run(self) -> None:
        log.info("watchdog: starting (degraded=%ds close=%ds tick=%ds)",
                 self.degraded_after_s, self.close_after_s, self.tick_interval_s)
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:  # pragma: no cover - log and continue
                log.exception("watchdog tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick_interval_s)
            except TimeoutError:
                continue
        log.info("watchdog: stopped")

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="meet-watchdog")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except TimeoutError:
                self._task.cancel()
            self._task = None
