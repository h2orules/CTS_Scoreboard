"""Socket.IO namespace handlers for the relay.

Two namespaces:

- ``/pi`` — authenticated, one-per-meet upstream from a Pi.
- ``/scoreboard`` — anonymous browser viewers; auth is the meet_id alone.

The Pi side stays the source of truth for meet content; this module only
mirrors state to Redis and fans out to browser viewers in the meet's room.
"""
from __future__ import annotations

import logging
from typing import Any

import socketio

from app import PROTOCOL_VERSION_CURRENT, PROTOCOL_VERSION_MIN_SUPPORTED
from app.auth import InvalidPiTokenError, validate_pi_token
from app.state import MeetStateStore

log = logging.getLogger(__name__)

# Custom session keys (kept intentionally small).
_SESSION_PI_MEET = "pi_meet_id"
_SESSION_PI_OID = "pi_account_id"
_SESSION_BROWSER_MEET = "browser_meet_id"


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
        log.info("pi connect: meet=%s upn=%s sid=%s", meet_id, identity.upn, sid)

    @sio.event(namespace="/pi")
    async def disconnect(sid: str) -> None:
        sess = await sio.get_session(sid, namespace="/pi")
        meet_id = sess.get(_SESSION_PI_MEET)
        if meet_id:
            store.mark_status(meet_id, "degraded")
            # Tell viewers their feed went dark.
            await sio.emit(
                "feed_status",
                {"status": "degraded"},
                room=meet_id,
                namespace="/scoreboard",
            )
            log.info("pi disconnect: meet=%s sid=%s", meet_id, sid)

    @sio.on("meet_open", namespace="/pi")
    async def on_meet_open(sid: str, payload: dict[str, Any]) -> dict[str, Any]:
        sess = await sio.get_session(sid, namespace="/pi")
        meet_id = sess.get(_SESSION_PI_MEET)
        if not meet_id or payload.get("meet_id") != meet_id:
            return {"ok": False, "error": "meet_id mismatch"}
        store.open_meet(
            meet_id,
            host_team_name=payload.get("host_team_name", ""),
            protocol_version=int(payload.get("protocol_version", PROTOCOL_VERSION_CURRENT)),
            pi_account_id=sess.get(_SESSION_PI_OID, ""),
        )
        # Notify any already-connected viewers that the feed is live.
        await sio.emit("feed_status", {"status": "live"}, room=meet_id, namespace="/scoreboard")
        return {"ok": True}

    @sio.on("update_scoreboard", namespace="/pi")
    async def on_update_scoreboard(sid: str, payload: dict[str, Any]) -> None:
        sess = await sio.get_session(sid, namespace="/pi")
        meet_id = sess.get(_SESSION_PI_MEET)
        if not meet_id:
            return
        store.put_state(meet_id, payload)
        await sio.emit("update_scoreboard", payload, room=meet_id, namespace="/scoreboard")

    @sio.on("event_info", namespace="/pi")
    async def on_event_info(sid: str, payload: dict[str, Any]) -> None:
        sess = await sio.get_session(sid, namespace="/pi")
        meet_id = sess.get(_SESSION_PI_MEET)
        if not meet_id:
            return
        # Treat event_info as part of state so reconnecting browsers hydrate.
        store.put_state(meet_id, {"event_info": payload})
        await sio.emit("event_info", payload, room=meet_id, namespace="/scoreboard")

    @sio.on("scores_info", namespace="/pi")
    async def on_scores_info(sid: str, payload: dict[str, Any]) -> None:
        sess = await sio.get_session(sid, namespace="/pi")
        meet_id = sess.get(_SESSION_PI_MEET)
        if not meet_id:
            return
        store.put_state(meet_id, {"scores_info": payload})
        await sio.emit("scores_info", payload, room=meet_id, namespace="/scoreboard")

    @sio.on("message_overlay_state", namespace="/pi")
    async def on_message_overlay_state(sid: str, payload: dict[str, Any]) -> None:
        sess = await sio.get_session(sid, namespace="/pi")
        meet_id = sess.get(_SESSION_PI_MEET)
        if not meet_id:
            return
        store.put_state(meet_id, {"message_overlay_state": payload})
        await sio.emit("message_overlay_state", payload, room=meet_id, namespace="/scoreboard")

    @sio.on("template_push", namespace="/pi")
    async def on_template_push(sid: str, bundle: dict[str, Any]) -> dict[str, Any]:
        sess = await sio.get_session(sid, namespace="/pi")
        meet_id = sess.get(_SESSION_PI_MEET)
        if not meet_id:
            return {"ok": False, "error": "no meet"}
        bundle_id = store.put_template(meet_id, bundle)
        # Tell browsers a new template is available; they re-fetch via HTTP.
        await sio.emit(
            "template_changed",
            {"bundle_id": bundle_id},
            room=meet_id,
            namespace="/scoreboard",
        )
        return {"ok": True, "bundle_id": bundle_id}

    @sio.on("meet_context", namespace="/pi")
    async def on_meet_context(sid: str, context: dict[str, Any]) -> dict[str, Any]:
        sess = await sio.get_session(sid, namespace="/pi")
        meet_id = sess.get(_SESSION_PI_MEET)
        if not meet_id:
            return {"ok": False, "error": "no meet"}
        store.put_context(meet_id, context)
        return {"ok": True}

    @sio.on("invalidate", namespace="/pi")
    async def on_invalidate(sid: str, payload: dict[str, Any]) -> dict[str, Any]:
        sess = await sio.get_session(sid, namespace="/pi")
        meet_id = sess.get(_SESSION_PI_MEET)
        if not meet_id:
            return {"ok": False, "error": "no meet"}
        names = payload.get("fragments") or []
        removed = store.invalidate_fragments(meet_id, list(names))
        await sio.emit(
            "invalidate",
            {"fragments": list(names)},
            room=meet_id,
            namespace="/scoreboard",
        )
        return {"ok": True, "removed": removed}

    @sio.on("heartbeat", namespace="/pi")
    async def on_heartbeat(sid: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
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
        sess = await sio.get_session(sid, namespace="/pi")
        meet_id = sess.get(_SESSION_PI_MEET)
        if not meet_id:
            return {"ok": False}
        store.close_meet(meet_id)
        await sio.emit("meet_closed", {"meet_id": meet_id}, room=meet_id, namespace="/scoreboard")
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
        return None
