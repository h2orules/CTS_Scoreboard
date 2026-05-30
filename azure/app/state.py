"""Hot-state store (Redis) for live meet data.

Phase 3: full implementation. Keys are always namespaced by meet_id so
multi-meet hosting on a single relay deployment stays clean.

Key scheme:
    meet:{id}:state              Redis hash, fields = update_scoreboard payload
                                 entries (each value is orjson-serialized)
    meet:{id}:metadata           JSON: {host_team_name, opened_at, last_heartbeat,
                                         protocol_version, status,
                                         template_bundle_id}
    meet:{id}:fragment:{name}    JSON: {key, html}
    meet:{id}:template:{bid}     JSON: {template_text, static_files (b64),
                                         partial_files}
    meet:{id}:current_template   String: bundle_id of the active template
    pi:{account_id}:meet_id      Reverse index: which meet ID a Pi identity owns

All store methods are async — the hot path runs on the asyncio event loop
and any sync Redis call would block the loop for the duration of the
round-trip, head-of-line-blocking every other coroutine on that worker.
"""
from __future__ import annotations

import functools
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeVar

import orjson
from redis.exceptions import ResponseError

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])

MeetStatus = Literal["live", "degraded", "closed", "expired_id_rotated"]

# Default TTLs (seconds).
STATE_TTL = 24 * 3600
METADATA_TTL = 14 * 24 * 3600  # keep metadata 2 weeks for "no live meet" pages
TEMPLATE_TTL = 24 * 3600
FRAGMENT_TTL = 24 * 3600

# Defaults for per-replica read caches (overridable via Settings + store ctor).
# A non-zero value enables that cache; 0 disables it entirely.
DEFAULT_FRAGMENT_CACHE_TTL = 1.0
DEFAULT_CURRENT_TEMPLATE_CACHE_TTL = 2.0
DEFAULT_TEMPLATE_BLOB_CACHE_MAX = 16  # bundles are immutable per bundle_id


class _TTLCache:
    """Tiny dict-based cache with optional TTL and bounded size.

    Used for per-replica caching of read-mostly Redis values. Safe for
    single-event-loop access (which is what asyncio gives us per worker).

    - ``enabled=False`` short-circuits both get and set so the cache is inert.
    - ``ttl=0`` means "no expiry" (only used for immutable entries).
    - ``ttl>0`` expires entries that many seconds after insertion.
    - When ``max_entries`` is reached, the oldest insertion is evicted
      (FIFO; good enough for our small keyspaces).
    """

    __slots__ = ("_clock", "_data", "_enabled", "_max", "_ttl")

    def __init__(
        self,
        *,
        ttl: float,
        max_entries: int = 1024,
        enabled: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._data: dict[Any, tuple[float, Any]] = {}
        self._ttl = float(ttl)
        self._max = int(max_entries)
        self._enabled = bool(enabled) and self._max > 0
        self._clock = clock

    def get(self, key: Any) -> Any | None:
        """Return cached value or ``None``. Missing/expired entries return None.

        ``None`` is reserved as the miss sentinel; do not cache None values.
        """
        if not self._enabled:
            return None
        entry = self._data.get(key)
        if entry is None:
            return None
        exp, value = entry
        if exp and self._clock() >= exp:
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: Any, value: Any) -> None:
        if value is None or not self._enabled:
            return
        if len(self._data) >= self._max:
            # FIFO eviction: drop the oldest insertion. dict preserves order.
            try:
                oldest_key = next(iter(self._data))
                self._data.pop(oldest_key, None)
            except StopIteration:
                pass
        exp = (self._clock() + self._ttl) if self._ttl > 0 else 0.0
        self._data[key] = (exp, value)

    def evict(self, key: Any) -> None:
        self._data.pop(key, None)

    def evict_prefix(self, predicate: Callable[[Any], bool]) -> None:
        """Drop every entry whose key matches ``predicate``."""
        for k in [k for k in self._data if predicate(k)]:
            self._data.pop(k, None)

    def clear(self) -> None:
        self._data.clear()


@dataclass(frozen=True)
class MeetKeys:
    """Helper to build the Redis key namespace for one meet."""

    meet_id: str

    @property
    def state(self) -> str:
        return f"meet:{self.meet_id}:state"

    @property
    def metadata(self) -> str:
        return f"meet:{self.meet_id}:metadata"

    @property
    def current_template(self) -> str:
        return f"meet:{self.meet_id}:current_template"

    @property
    def context(self) -> str:
        return f"meet:{self.meet_id}:context"

    def fragment(self, name: str) -> str:
        return f"meet:{self.meet_id}:fragment:{name}"

    def template(self, bundle_id: str) -> str:
        return f"meet:{self.meet_id}:template:{bundle_id}"


class AsyncRedisLike(Protocol):
    """Minimal subset of redis.asyncio we use; satisfied by fakeredis.aioredis."""

    async def get(self, key: str) -> Any: ...
    async def set(self, key: str, value: Any, ex: int | None = ...) -> Any: ...
    async def delete(self, *keys: str) -> Any: ...
    async def exists(self, *keys: str) -> Any: ...
    async def hset(self, key: str, mapping: dict[str, Any] | None = ...) -> Any: ...
    async def hgetall(self, key: str) -> Any: ...
    async def expire(self, key: str, seconds: int) -> Any: ...
    def pipeline(self, transaction: bool = ...) -> Any: ...
    def scan_iter(self, match: str | None = ...) -> AsyncIterator[Any]: ...


def _maybe_str(v: Any) -> str | None:
    """Decode bytes to str; pass through str; None stays None."""
    if v is None:
        return None
    if isinstance(v, bytes):
        return v.decode("utf-8")
    return v  # type: ignore[return-value]


def _timed(op: str) -> Callable[[F], F]:
    """Record the wrapped async MeetStateStore method's latency in seconds.

    Imports ``get_metrics`` lazily to avoid a circular import at module load
    and to keep the path inert when telemetry hasn't been configured yet.
    """

    def deco(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            from app.telemetry import get_metrics

            metrics = get_metrics()
            t0 = time.perf_counter()
            try:
                return await fn(self, *args, **kwargs)
            finally:
                metrics.redis_op_seconds.record(
                    time.perf_counter() - t0, {"op": op}
                )

        return wrapper  # type: ignore[return-value]

    return deco


class MeetStateStore:
    """High-level Redis-backed store for one relay deployment."""

    def __init__(
        self,
        redis: AsyncRedisLike,
        *,
        clock: Callable[[], float] = time.time,
        fragment_cache_ttl: float = DEFAULT_FRAGMENT_CACHE_TTL,
        current_template_cache_ttl: float = DEFAULT_CURRENT_TEMPLATE_CACHE_TTL,
        template_blob_cache_max: int = DEFAULT_TEMPLATE_BLOB_CACHE_MAX,
    ) -> None:
        self._r = redis
        self._clock = clock
        # Per-replica read caches. Keys are tuples so we can scope by meet_id.
        self._fragment_cache = _TTLCache(
            ttl=fragment_cache_ttl,
            max_entries=2048,
            enabled=fragment_cache_ttl > 0,
        )
        self._current_template_cache = _TTLCache(
            ttl=current_template_cache_ttl,
            max_entries=512,
            enabled=current_template_cache_ttl > 0,
        )
        # Template bundles are immutable per bundle_id (write-once in
        # put_template), so we cache without TTL and bound by entry count.
        self._template_blob_cache = _TTLCache(
            ttl=0.0,
            max_entries=template_blob_cache_max,
            enabled=template_blob_cache_max > 0,
        )

    def _record_cache_hit(self, op: str) -> None:
        from app.telemetry import get_metrics

        get_metrics().cache_hits.add(1, {"op": op})

    # ---------- meet lifecycle ----------

    @_timed("open_meet")
    async def open_meet(
        self,
        meet_id: str,
        *,
        host_team_name: str,
        protocol_version: int,
        pi_account_id: str,
    ) -> None:
        """Mark a meet as live. Idempotent: re-opening just refreshes the
        heartbeat and Pi binding."""
        k = MeetKeys(meet_id)
        existing = await self.get_metadata(meet_id)
        opened_at = (existing or {}).get("opened_at") or self._clock()
        meta = {
            "meet_id": meet_id,
            "host_team_name": host_team_name,
            "protocol_version": protocol_version,
            "status": "live",
            "opened_at": opened_at,
            "last_heartbeat": self._clock(),
            "pi_account_id": pi_account_id,
        }
        async with self._r.pipeline(transaction=False) as pipe:
            pipe.set(k.metadata, orjson.dumps(meta), ex=METADATA_TTL)
            pipe.set(f"pi:{pi_account_id}:meet_id", meet_id, ex=METADATA_TTL)
            await pipe.execute()

    @_timed("heartbeat")
    async def heartbeat(self, meet_id: str) -> None:
        meta = await self.get_metadata(meet_id)
        if not meta:
            return
        meta["last_heartbeat"] = self._clock()
        if meta.get("status") == "degraded":
            meta["status"] = "live"
        await self._r.set(MeetKeys(meet_id).metadata, orjson.dumps(meta), ex=METADATA_TTL)

    @_timed("mark_status")
    async def mark_status(self, meet_id: str, status: MeetStatus) -> None:
        meta = await self.get_metadata(meet_id)
        if not meta:
            return
        meta["status"] = status
        if status == "closed":
            meta["closed_at"] = self._clock()
        await self._r.set(MeetKeys(meet_id).metadata, orjson.dumps(meta), ex=METADATA_TTL)

    @_timed("get_metadata")
    async def get_metadata(self, meet_id: str) -> dict[str, Any] | None:
        raw = await self._r.get(MeetKeys(meet_id).metadata)
        if not raw:
            return None
        try:
            return orjson.loads(raw)
        except orjson.JSONDecodeError:
            return None

    # ---------- pi <-> meet binding (used by friendly-name picker) ----------

    @_timed("get_pi_meet_id")
    async def get_pi_meet_id(self, pi_account_id: str) -> str | None:
        """Return the meet ID currently bound to this Pi identity, if any."""
        if not pi_account_id:
            return None
        return _maybe_str(await self._r.get(f"pi:{pi_account_id}:meet_id"))

    @_timed("is_meet_id_taken")
    async def is_meet_id_taken(
        self, meet_id: str, *, by_account_id: str
    ) -> Literal["no", "self", "other"]:
        """Check whether ``meet_id`` is already claimed.

        - ``"no"``: no metadata exists for this id (truly free, e.g. TTL
          elapsed), or a rotated/closed record exists but has no owner
          recorded (orphan — back-compat for records written before the
          ``pi_account_id`` field existed).
        - ``"self"``: the current Pi (``by_account_id``) owns the record.
          This includes records marked ``expired_id_rotated`` or
          ``closed`` — the original owner may reclaim their own name
          until the metadata TTL elapses.
        - ``"other"``: a different Pi owns the record (live, closed, or
          rotated), OR live/closed metadata exists but is orphaned (no
          recorded ``pi_account_id``); treated as taken so we fail safely.
        """
        meta = await self.get_metadata(meet_id)
        if not meta:
            return "no"
        owner = meta.get("pi_account_id") or ""
        # Orphaned rotated/closed records (no owner recorded) are free for
        # anyone to claim — there is nobody to gate reclaim against. Live
        # orphaned records still fall through below and are treated as
        # taken, since handing a live id to someone new would be unsafe.
        if not owner and meta.get("status") == "expired_id_rotated":
            return "no"
        if owner and by_account_id and owner == by_account_id:
            return "self"
        return "other"

    # ---------- live state (latest update_scoreboard payload) ----------

    @_timed("put_state")
    async def put_state(self, meet_id: str, payload: dict[str, Any]) -> None:
        """Field-level update: merge ``payload`` into the existing state hash.

        Uses HSET so partial updates accumulate (mirrors how the home.html
        client builds up ``s[k] = v``) without the previous read-modify-write
        cost on every Pi event. Pipelined with EXPIRE so the whole op is one
        round-trip to Redis.
        """
        if not payload:
            return
        key = MeetKeys(meet_id).state
        mapping = {k: orjson.dumps(v) for k, v in payload.items()}
        try:
            async with self._r.pipeline(transaction=False) as pipe:
                pipe.hset(key, mapping=mapping)
                pipe.expire(key, STATE_TTL)
                await pipe.execute()
        except ResponseError as e:
            # Legacy state keys (pre-Phase-7) were stored as a single JSON
            # string. HSET on a non-hash key raises WRONGTYPE. Drop the
            # stale key and retry once — we lose at most the last cached
            # snapshot, which the next Pi event repopulates within seconds.
            if "WRONGTYPE" not in str(e):
                raise
            await self._r.delete(key)
            async with self._r.pipeline(transaction=False) as pipe:
                pipe.hset(key, mapping=mapping)
                pipe.expire(key, STATE_TTL)
                await pipe.execute()

    @_timed("get_state")
    async def get_state(self, meet_id: str) -> dict[str, Any] | None:
        raw = await self._r.hgetall(MeetKeys(meet_id).state)
        if not raw:
            return None
        out: dict[str, Any] = {}
        for k, v in raw.items():
            key_str = k.decode("utf-8") if isinstance(k, bytes) else k
            try:
                out[key_str] = orjson.loads(v)
            except orjson.JSONDecodeError:
                # Field stored as non-JSON (shouldn't happen with our writer,
                # but tolerate it so one bad field doesn't black-hole the meet).
                out[key_str] = _maybe_str(v)
        return out

    # ---------- fragments (HTML chunks with ETag-style keys) ----------

    @_timed("put_fragment")
    async def put_fragment(self, meet_id: str, name: str, key: str, html: str) -> None:
        await self._r.set(
            MeetKeys(meet_id).fragment(name),
            orjson.dumps({"key": key, "html": html}),
            ex=FRAGMENT_TTL,
        )
        # Refresh the local cache so this replica serves the new value
        # immediately. Other replicas pick it up once their TTL elapses.
        self._fragment_cache.set((meet_id, name), (key, html))

    async def get_fragment(self, meet_id: str, name: str) -> tuple[str, str] | None:
        cached = self._fragment_cache.get((meet_id, name))
        if cached is not None:
            self._record_cache_hit("get_fragment")
            return cached
        value = await self._get_fragment_uncached(meet_id, name)
        if value is not None:
            self._fragment_cache.set((meet_id, name), value)
        return value

    @_timed("get_fragment")
    async def _get_fragment_uncached(
        self, meet_id: str, name: str
    ) -> tuple[str, str] | None:
        raw = await self._r.get(MeetKeys(meet_id).fragment(name))
        if not raw:
            return None
        try:
            d = orjson.loads(raw)
            return d["key"], d["html"]
        except (orjson.JSONDecodeError, KeyError):
            return None

    @_timed("invalidate_fragments")
    async def invalidate_fragments(self, meet_id: str, names: list[str]) -> int:
        """Delete the named fragments. Returns number of keys removed."""
        if not names:
            return 0
        keys = [MeetKeys(meet_id).fragment(n) for n in names]
        for n in names:
            self._fragment_cache.evict((meet_id, n))
        return int(await self._r.delete(*keys))

    # ---------- templates ----------

    @_timed("put_template")
    async def put_template(self, meet_id: str, bundle: dict[str, Any]) -> str:
        """Store a bundle (idempotent on bundle_id) and mark it as current.

        Returns the bundle_id."""
        bundle_id = str(bundle["bundle_id"])
        k = MeetKeys(meet_id).template(bundle_id)
        if not await self._r.exists(k):
            await self._r.set(k, orjson.dumps(bundle), ex=TEMPLATE_TTL)
        await self._r.set(MeetKeys(meet_id).current_template, bundle_id, ex=TEMPLATE_TTL)
        # Refresh local caches so subsequent reads on this replica are
        # consistent with what we just wrote.
        self._template_blob_cache.set((meet_id, bundle_id), bundle)
        self._current_template_cache.set(meet_id, bundle)
        return bundle_id

    async def get_current_template(self, meet_id: str) -> dict[str, Any] | None:
        cached = self._current_template_cache.get(meet_id)
        if cached is not None:
            self._record_cache_hit("get_current_template")
            return cached
        value = await self._get_current_template_uncached(meet_id)
        if value is not None:
            self._current_template_cache.set(meet_id, value)
        return value

    @_timed("get_current_template")
    async def _get_current_template_uncached(
        self, meet_id: str
    ) -> dict[str, Any] | None:
        bundle_id = _maybe_str(await self._r.get(MeetKeys(meet_id).current_template))
        if not bundle_id:
            return None
        # Reuse the bundle cache if we already have it — the bundle blob is
        # immutable per bundle_id so a stale hit is impossible.
        cached_blob = self._template_blob_cache.get((meet_id, bundle_id))
        if cached_blob is not None:
            self._record_cache_hit("get_template_blob")
            return cached_blob
        raw = await self._r.get(MeetKeys(meet_id).template(bundle_id))
        if not raw:
            return None
        try:
            bundle = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return None
        self._template_blob_cache.set((meet_id, bundle_id), bundle)
        return bundle

    async def get_template_blob(
        self, meet_id: str, bundle_id: str
    ) -> dict[str, Any] | None:
        """Fetch a specific template bundle by id. Used by the static-asset
        route to serve from a pinned older bundle when a browser is still
        on an earlier revision."""
        cached = self._template_blob_cache.get((meet_id, bundle_id))
        if cached is not None:
            self._record_cache_hit("get_template_blob")
            return cached
        value = await self._get_template_blob_uncached(meet_id, bundle_id)
        if value is not None:
            self._template_blob_cache.set((meet_id, bundle_id), value)
        return value

    @_timed("get_template_blob")
    async def _get_template_blob_uncached(
        self, meet_id: str, bundle_id: str
    ) -> dict[str, Any] | None:
        raw = await self._r.get(MeetKeys(meet_id).template(bundle_id))
        if not raw:
            return None
        try:
            return orjson.loads(raw)
        except orjson.JSONDecodeError:
            return None

    # ---------- close ----------

    @_timed("close_meet")
    async def close_meet(self, meet_id: str) -> None:
        """Close the meet: keep metadata (for 'no live meet' pages), drop hot
        state and fragments."""
        await self.mark_status(meet_id, "closed")
        k = MeetKeys(meet_id)
        await self._r.delete(k.state, k.current_template)
        # Drop every cached entry that belongs to this meet.
        self._current_template_cache.evict(meet_id)
        self._fragment_cache.evict_prefix(lambda key: key[0] == meet_id)
        self._template_blob_cache.evict_prefix(lambda key: key[0] == meet_id)

    # ---------- render context (Phase 4) ----------
    @_timed("put_context")
    async def put_context(self, meet_id: str, context: dict[str, Any]) -> None:
        """Store the initial render context. Replaces the previous snapshot."""
        await self._r.set(
            MeetKeys(meet_id).context,
            # default=str matches the previous json.dumps(..., default=str)
            # behavior so datetimes etc. don't explode.
            orjson.dumps(context, default=str),
            ex=METADATA_TTL,
        )

    @_timed("get_context")
    async def get_context(self, meet_id: str) -> dict[str, Any] | None:
        raw = await self._r.get(MeetKeys(meet_id).context)
        if not raw:
            return None
        try:
            return orjson.loads(raw)
        except orjson.JSONDecodeError:
            return None

    # ---------- enumeration (Phase 6) ----------

    async def iter_active_meet_ids(self) -> AsyncIterator[str]:
        """Yield the meet IDs of every meet whose metadata is still in store.

        Uses SCAN so it's safe on production Redis even with many keys.
        """
        async for raw_key in self._r.scan_iter(match="meet:*:metadata"):
            key = _maybe_str(raw_key) or ""
            # key shape: meet:<id>:metadata
            parts = key.split(":")
            if len(parts) >= 3 and parts[0] == "meet" and parts[-1] == "metadata":
                yield ":".join(parts[1:-1])
