"""Hot-state store (Redis) for live meet data.

Phase 3: full implementation. Keys are always namespaced by meet_id so
multi-meet hosting on a single relay deployment stays clean.

Key scheme:
    meet:{id}:state              JSON of latest update_scoreboard payload
    meet:{id}:metadata           JSON: {host_team_name, opened_at, last_heartbeat,
                                         protocol_version, status,
                                         template_bundle_id}
    meet:{id}:fragment:{name}    JSON: {key, html}
    meet:{id}:template:{bid}     JSON: {template_text, static_files (b64),
                                         partial_files}
    meet:{id}:current_template   String: bundle_id of the active template
    pi:{account_id}:meet_id      Reverse index: which meet ID a Pi identity owns
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Literal, Protocol

MeetStatus = Literal["live", "degraded", "closed", "expired_id_rotated"]

# Default TTLs (seconds).
STATE_TTL = 24 * 3600
METADATA_TTL = 14 * 24 * 3600  # keep metadata 2 weeks for "no live meet" pages
TEMPLATE_TTL = 24 * 3600
FRAGMENT_TTL = 24 * 3600


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


class RedisLike(Protocol):
    """Minimal subset of redis-py we use; satisfied by fakeredis too."""

    def get(self, key: str) -> Any: ...
    def set(self, key: str, value: Any, ex: int | None = ...) -> Any: ...
    def delete(self, *keys: str) -> Any: ...
    def exists(self, *keys: str) -> Any: ...


def _maybe_str(v: Any) -> str | None:
    """Decode bytes to str; pass through str; None stays None."""
    if v is None:
        return None
    if isinstance(v, bytes):
        return v.decode("utf-8")
    return v  # type: ignore[return-value]


class MeetStateStore:
    """High-level Redis-backed store for one relay deployment."""

    def __init__(self, redis: RedisLike, *, clock: callable = time.time) -> None:
        self._r = redis
        self._clock = clock

    # ---------- meet lifecycle ----------

    def open_meet(
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
        existing = self.get_metadata(meet_id)
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
        self._r.set(k.metadata, json.dumps(meta), ex=METADATA_TTL)
        self._r.set(f"pi:{pi_account_id}:meet_id", meet_id, ex=METADATA_TTL)

    def heartbeat(self, meet_id: str) -> None:
        meta = self.get_metadata(meet_id)
        if not meta:
            return
        meta["last_heartbeat"] = self._clock()
        if meta.get("status") == "degraded":
            meta["status"] = "live"
        self._r.set(MeetKeys(meet_id).metadata, json.dumps(meta), ex=METADATA_TTL)

    def mark_status(self, meet_id: str, status: MeetStatus) -> None:
        meta = self.get_metadata(meet_id)
        if not meta:
            return
        meta["status"] = status
        if status == "closed":
            meta["closed_at"] = self._clock()
        self._r.set(MeetKeys(meet_id).metadata, json.dumps(meta), ex=METADATA_TTL)

    def get_metadata(self, meet_id: str) -> dict[str, Any] | None:
        raw = _maybe_str(self._r.get(MeetKeys(meet_id).metadata))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    # ---------- live state (latest update_scoreboard payload) ----------

    def put_state(self, meet_id: str, payload: dict[str, Any]) -> None:
        # Merge into existing state so partial updates accumulate, mirroring
        # how the home.html client builds up s[k] = v.
        existing = self.get_state(meet_id) or {}
        existing.update(payload)
        self._r.set(MeetKeys(meet_id).state, json.dumps(existing), ex=STATE_TTL)

    def get_state(self, meet_id: str) -> dict[str, Any] | None:
        raw = _maybe_str(self._r.get(MeetKeys(meet_id).state))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    # ---------- fragments (HTML chunks with ETag-style keys) ----------

    def put_fragment(self, meet_id: str, name: str, key: str, html: str) -> None:
        self._r.set(
            MeetKeys(meet_id).fragment(name),
            json.dumps({"key": key, "html": html}),
            ex=FRAGMENT_TTL,
        )

    def get_fragment(self, meet_id: str, name: str) -> tuple[str, str] | None:
        raw = _maybe_str(self._r.get(MeetKeys(meet_id).fragment(name)))
        if not raw:
            return None
        try:
            d = json.loads(raw)
            return d["key"], d["html"]
        except (json.JSONDecodeError, KeyError):
            return None

    def invalidate_fragments(self, meet_id: str, names: list[str]) -> int:
        """Delete the named fragments. Returns number of keys removed."""
        if not names:
            return 0
        keys = [MeetKeys(meet_id).fragment(n) for n in names]
        return int(self._r.delete(*keys))

    # ---------- templates ----------

    def put_template(self, meet_id: str, bundle: dict[str, Any]) -> str:
        """Store a bundle (idempotent on bundle_id) and mark it as current.

        Returns the bundle_id."""
        bundle_id = str(bundle["bundle_id"])
        k = MeetKeys(meet_id).template(bundle_id)
        if not self._r.exists(k):
            self._r.set(k, json.dumps(bundle), ex=TEMPLATE_TTL)
        self._r.set(MeetKeys(meet_id).current_template, bundle_id, ex=TEMPLATE_TTL)
        return bundle_id

    def get_current_template(self, meet_id: str) -> dict[str, Any] | None:
        bundle_id = _maybe_str(self._r.get(MeetKeys(meet_id).current_template))
        if not bundle_id:
            return None
        raw = _maybe_str(self._r.get(MeetKeys(meet_id).template(bundle_id)))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    # ---------- close ----------

    def close_meet(self, meet_id: str) -> None:
        """Close the meet: keep metadata (for 'no live meet' pages), drop hot
        state and fragments."""
        self.mark_status(meet_id, "closed")
        k = MeetKeys(meet_id)
        self._r.delete(k.state, k.current_template)

    # ---------- render context (Phase 4) ----------
    def put_context(self, meet_id: str, context: dict[str, Any]) -> None:
        """Store the initial render context. Replaces the previous snapshot."""
        self._r.set(
            MeetKeys(meet_id).context,
            json.dumps(context, default=str),
            ex=METADATA_TTL,
        )

    def get_context(self, meet_id: str) -> dict[str, Any] | None:
        raw = _maybe_str(self._r.get(MeetKeys(meet_id).context))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    # ---------- enumeration (Phase 6) ----------

    def iter_active_meet_ids(self):
        """Yield the meet IDs of every meet whose metadata is still in store.

        Uses SCAN (via redis-py / fakeredis ``scan_iter``) so it's safe on
        production Redis even with many keys.
        """
        for raw_key in self._r.scan_iter(match="meet:*:metadata"):
            key = _maybe_str(raw_key) or ""
            # key shape: meet:<id>:metadata
            parts = key.split(":")
            if len(parts) >= 3 and parts[0] == "meet" and parts[-1] == "metadata":
                yield ":".join(parts[1:-1])
