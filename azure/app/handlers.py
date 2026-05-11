"""Socket.IO namespace handlers for the relay.

Two namespaces:

- ``/pi`` — authenticated, one-per-meet upstream from a Pi.
- ``/scoreboard`` — anonymous browser viewers; auth is the meet_id alone.

The Pi side stays the source of truth for meet content; this module only
mirrors state to Redis and fans out to browser viewers in the meet's room.
"""
from __future__ import annotations

import logging
import re
from contextlib import AbstractContextManager
from typing import Any

import socketio

from app import PROTOCOL_VERSION_CURRENT, PROTOCOL_VERSION_MIN_SUPPORTED
from app.auth import InvalidPiTokenError, validate_pi_token
from app.state import MeetStateStore
from app.telemetry import get_metrics, record_latency

log = logging.getLogger(__name__)

# Custom session keys (kept intentionally small).
_SESSION_PI_MEET = "pi_meet_id"
_SESSION_PI_OID = "pi_account_id"
_SESSION_BROWSER_MEET = "browser_meet_id"

# Mirrors azure_relay.MEET_ID_REGEX and routes.MEET_ID_RE.
_MEET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,20}$")


def register_handlers(
    sio: socketio.AsyncServer,
    *,
    store: MeetStateStore,
    tenant_id: str,
    audience: str,
    token_validator=None,
) -> None:
    """Register all namespace handlers on the given AsyncServer.

    ``token_validator`` is overridable for tests; defaults to
    :func:`app.auth.validate_pi_token`."""
    validator = token_validator or (
        lambda token: validate_pi_token(token, tenant_id=tenant_id, audience=audience)
    )
    metrics = get_metrics()

    def _handler_timer(event: str) -> AbstractContextManager[None]:
        return record_latency(
            metrics.event_handler_seconds, {"event": event, "namespace": "/pi"}
        )

    def _emit_timer(event: str) -> AbstractContextManager[None]:
        return record_latency(
            metrics.emit_fanout_seconds,
            {"event": event, "namespace": "/scoreboard"},
        )

    # ============================================================
    # /pi namespace - upstream from the Raspberry Pi
    # ============================================================

    @sio.event(namespace="/pi")
    async def connect(sid: str, environ: dict[str, Any], auth: dict[str, Any] | None = None) -> None:
        if not auth or not isinstance(auth, dict):
            log.warning("pi connect: missing auth (sid=%s)", sid)
            raise socketio.exceptions.ConnectionRefusedError("missing auth")

        token = auth.get("access_token")
        meet_id = auth.get("meet_id")
        proto = auth.get("protocol_version")

        if not meet_id:
            raise socketio.exceptions.ConnectionRefusedError("missing meet_id")
        if not _MEET_ID_RE.match(meet_id):
            raise socketio.exceptions.ConnectionRefusedError("invalid meet_id")
        if not isinstance(proto, int) or proto < PROTOCOL_VERSION_MIN_SUPPORTED or proto > PROTOCOL_VERSION_CURRENT:
            raise socketio.exceptions.ConnectionRefusedError(f"unsupported protocol_version={proto}")

        try:
            identity = validator(token)
        except InvalidPiTokenError as exc:
            log.warning("pi connect: token rejected sid=%s: %s", sid, exc)
            raise socketio.exceptions.ConnectionRefusedError("invalid_token") from exc

        await sio.save_session(
            sid,
            {_SESSION_PI_MEET: meet_id, _SESSION_PI_OID: identity.account_id},
            namespace="/pi",
        )
        # Pi sits in its own room (one Pi per meet), so we can target it later.
        await sio.enter_room(sid, f"pi:{meet_id}", namespace="/pi")
        metrics.active_sockets.add(1, {"namespace": "/pi"})
        metrics.pi_connections.add(1)
        log.info("pi connect: meet=%s upn=%s sid=%s", meet_id, identity.upn, sid)

    @sio.event(namespace="/pi")
    async def disconnect(sid: str) -> None:
        sess = await sio.get_session(sid, namespace="/pi")
        meet_id = sess.get(_SESSION_PI_MEET)
        if meet_id:
            store.mark_status(meet_id, "degraded")
            # Tell viewers their feed went dark.
            with _emit_timer("feed_status"):
                await sio.emit(
                    "feed_status",
                    {"status": "degraded"},
                    room=meet_id,
                    namespace="/scoreboard",
                )
            log.info("pi disconnect: meet=%s sid=%s", meet_id, sid)
        # Decrement even if meet_id is missing so the gauge stays balanced
        # against the increment in connect (which fires before any session
        # bookkeeping uses meet_id).
        metrics.active_sockets.add(-1, {"namespace": "/pi"})
        metrics.pi_connections.add(-1)

    @sio.on("meet_open", namespace="/pi")
    async def on_meet_open(sid: str, payload: dict[str, Any]) -> dict[str, Any]:
        with _handler_timer("meet_open"):
            sess = await sio.get_session(sid, namespace="/pi")
            meet_id = sess.get(_SESSION_PI_MEET)
            if not meet_id or payload.get("meet_id") != meet_id:
                return {"ok": False, "error": "meet_id mismatch"}
            pi_oid = sess.get(_SESSION_PI_OID, "")
            # Reject if this id is owned by a different Pi. Self-claim is fine
            # (idempotent re-open). "no" means it's free / metadata expired.
            taken = store.is_meet_id_taken(meet_id, by_account_id=pi_oid)
            if taken == "other":
                log.warning(
                    "pi meet_open: meet_id=%s already owned by another Pi (oid=%s)",
                    meet_id, pi_oid,
                )
                return {"ok": False, "error": "meet_id_taken"}
            # If the Pi previously owned a different id (friendly-name change or
            # Rotate Meet ID), mark the old id expired so old QR codes get the
            # friendly "Link expired" page.
            prev_id = store.get_pi_meet_id(pi_oid) if pi_oid else None
            if prev_id and prev_id != meet_id:
                store.mark_status(prev_id, "expired_id_rotated")
                log.info("pi meet_open: marked previous meet_id=%s expired_id_rotated", prev_id)
            store.open_meet(
                meet_id,
                host_team_name=payload.get("host_team_name", ""),
                protocol_version=int(payload.get("protocol_version", PROTOCOL_VERSION_CURRENT)),
                pi_account_id=pi_oid,
            )
            metrics.meet_opened.add(1, {"meet_id": meet_id})
            # Notify any already-connected viewers that the feed is live.
            with _emit_timer("feed_status"):
                await sio.emit("feed_status", {"status": "live"}, room=meet_id, namespace="/scoreboard")
            return {"ok": True}

    @sio.on("update_scoreboard", namespace="/pi")
    async def on_update_scoreboard(sid: str, payload: dict[str, Any]) -> None:
        with _handler_timer("update_scoreboard"):
            sess = await sio.get_session(sid, namespace="/pi")
            meet_id = sess.get(_SESSION_PI_MEET)
            if not meet_id:
                return
            store.put_state(meet_id, payload)
            metrics.relay_event_processed.add(1, {"event": "update_scoreboard"})
            with _emit_timer("update_scoreboard"):
                await sio.emit("update_scoreboard", payload, room=meet_id, namespace="/scoreboard")

    @sio.on("event_info", namespace="/pi")
    async def on_event_info(sid: str, payload: dict[str, Any]) -> None:
        with _handler_timer("event_info"):
            sess = await sio.get_session(sid, namespace="/pi")
            meet_id = sess.get(_SESSION_PI_MEET)
            if not meet_id:
                return
            # Treat event_info as part of state so reconnecting browsers hydrate.
            store.put_state(meet_id, {"event_info": payload})
            with _emit_timer("event_info"):
                await sio.emit("event_info", payload, room=meet_id, namespace="/scoreboard")

    @sio.on("scores_info", namespace="/pi")
    async def on_scores_info(sid: str, payload: dict[str, Any]) -> None:
        with _handler_timer("scores_info"):
            sess = await sio.get_session(sid, namespace="/pi")
            meet_id = sess.get(_SESSION_PI_MEET)
            if not meet_id:
                return
            store.put_state(meet_id, {"scores_info": payload})
            with _emit_timer("scores_info"):
                await sio.emit("scores_info", payload, room=meet_id, namespace="/scoreboard")

    @sio.on("message_overlay_state", namespace="/pi")
    async def on_message_overlay_state(sid: str, payload: dict[str, Any]) -> None:
        with _handler_timer("message_overlay_state"):
            sess = await sio.get_session(sid, namespace="/pi")
            meet_id = sess.get(_SESSION_PI_MEET)
            if not meet_id:
                return
            store.put_state(meet_id, {"message_overlay_state": payload})
            with _emit_timer("message_overlay_state"):
                await sio.emit(
                    "message_overlay_state",
                    payload,
                    room=meet_id,
                    namespace="/scoreboard",
                )

    @sio.on("template_push", namespace="/pi")
    async def on_template_push(sid: str, bundle: dict[str, Any]) -> dict[str, Any]:
        with _handler_timer("template_push"):
            sess = await sio.get_session(sid, namespace="/pi")
            meet_id = sess.get(_SESSION_PI_MEET)
            if not meet_id:
                return {"ok": False, "error": "no meet"}
            bundle_id = store.put_template(meet_id, bundle)
            # Tell browsers a new template is available; they re-fetch via HTTP.
            with _emit_timer("template_changed"):
                await sio.emit(
                    "template_changed",
                    {"bundle_id": bundle_id},
                    room=meet_id,
                    namespace="/scoreboard",
                )
            return {"ok": True, "bundle_id": bundle_id}

    @sio.on("meet_context", namespace="/pi")
    async def on_meet_context(sid: str, context: dict[str, Any]) -> dict[str, Any]:
        with _handler_timer("meet_context"):
            sess = await sio.get_session(sid, namespace="/pi")
            meet_id = sess.get(_SESSION_PI_MEET)
            if not meet_id:
                return {"ok": False, "error": "no meet"}
            store.put_context(meet_id, context)
            return {"ok": True}

    @sio.on("reload_clients", namespace="/pi")
    async def on_reload_clients(sid: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Pi requested all live viewers reload (Pi-side settings change).

        We don't try to be clever about which keys changed — the Pi
        already re-pushed meet_context before this event, so a soft reload
        on the connected browsers is enough for them to pick up the fresh
        server-rendered HTML.
        """
        with _handler_timer("reload_clients"):
            sess = await sio.get_session(sid, namespace="/pi")
            meet_id = sess.get(_SESSION_PI_MEET)
            if not meet_id:
                return {"ok": False, "error": "no meet"}
            with _emit_timer("reload_clients"):
                await sio.emit(
                    "reload_clients",
                    payload or {},
                    room=meet_id,
                    namespace="/scoreboard",
                )
            return {"ok": True}

    @sio.on("invalidate", namespace="/pi")
    async def on_invalidate(sid: str, payload: dict[str, Any]) -> dict[str, Any]:
        with _handler_timer("invalidate"):
            sess = await sio.get_session(sid, namespace="/pi")
            meet_id = sess.get(_SESSION_PI_MEET)
            if not meet_id:
                return {"ok": False, "error": "no meet"}
            names = payload.get("fragments") or []
            removed = store.invalidate_fragments(meet_id, list(names))
            with _emit_timer("invalidate"):
                await sio.emit(
                    "invalidate",
                    {"fragments": list(names)},
                    room=meet_id,
                    namespace="/scoreboard",
                )
            return {"ok": True, "removed": removed}

    @sio.on("fragment", namespace="/pi")
    async def on_fragment(sid: str, payload: dict[str, Any]) -> None:
        """Pi pushed a rendered HTML fragment (e.g. message_page_0).

        Stored in Redis so the browser can fetch it via /m/{meet_id}/api/...
        instead of trying to reach the Pi directly.
        """
        with _handler_timer("fragment"):
            sess = await sio.get_session(sid, namespace="/pi")
            meet_id = sess.get(_SESSION_PI_MEET)
            if not meet_id:
                return
            name = payload.get("name")
            key = payload.get("key")
            html = payload.get("html")
            if not name or not key or html is None:
                return
            store.put_fragment(meet_id, str(name), str(key), str(html))

    @sio.on("heartbeat", namespace="/pi")
    async def on_heartbeat(sid: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        with _handler_timer("heartbeat"):
            sess = await sio.get_session(sid, namespace="/pi")
            meet_id = sess.get(_SESSION_PI_MEET)
            if not meet_id:
                return {"ok": False}
            store.heartbeat(meet_id)
            # Count distinct browser viewers in the meet's room.
            try:
                participants = sio.manager.get_participants(namespace="/scoreboard", room=meet_id)
                count = sum(1 for _ in participants)
            except Exception:  # pragma: no cover - defensive
                count = 0
            return {"ok": True, "active_client_count": count}

    @sio.on("meet_close", namespace="/pi")
    async def on_meet_close(sid: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        with _handler_timer("meet_close"):
            sess = await sio.get_session(sid, namespace="/pi")
            meet_id = sess.get(_SESSION_PI_MEET)
            if not meet_id:
                return {"ok": False}
            store.close_meet(meet_id)
            with _emit_timer("meet_closed"):
                await sio.emit(
                    "meet_closed", {"meet_id": meet_id}, room=meet_id, namespace="/scoreboard"
                )
            return {"ok": True}

    # ============================================================
    # /scoreboard namespace - browser viewers
    # ============================================================

    @sio.event(namespace="/scoreboard")
    async def connect(sid: str, environ: dict[str, Any], auth: dict[str, Any] | None = None) -> None:  # noqa: F811
        meet_id = (auth or {}).get("meet_id") if isinstance(auth, dict) else None
        if not meet_id:
            # Allow connection without a meet so the home page can run before
            # the user picks a meet, but they can't join a room yet.
            return None
        meta = store.get_metadata(meet_id)
        if not meta:
            raise socketio.exceptions.ConnectionRefusedError("unknown_meet")
        await sio.enter_room(sid, meet_id, namespace="/scoreboard")
        await sio.save_session(sid, {_SESSION_BROWSER_MEET: meet_id}, namespace="/scoreboard")
        metrics.browser_connected.add(1, {"meet_id": meet_id})
        metrics.active_sockets.add(1, {"namespace": "/scoreboard"})
        # Hydrate the freshly connected browser with the latest state.
        state = store.get_state(meet_id)
        if state:
            await sio.emit("update_scoreboard", state, to=sid, namespace="/scoreboard")
        await sio.emit(
            "feed_status",
            {"status": meta.get("status", "live")},
            to=sid,
            namespace="/scoreboard",
        )

    @sio.event(namespace="/scoreboard")
    async def disconnect(sid: str) -> None:  # noqa: F811
        sess = await sio.get_session(sid, namespace="/scoreboard")
        meet_id = sess.get(_SESSION_BROWSER_MEET)
        if meet_id:
            metrics.browser_disconnected.add(1, {"meet_id": meet_id})
            metrics.active_sockets.add(-1, {"namespace": "/scoreboard"})
        return None
