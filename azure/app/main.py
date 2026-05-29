"""FastAPI + Socket.IO entrypoint for the relay app."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import redis as redis_sync
import socketio
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app import (
    PROTOCOL_VERSION_CURRENT,
    PROTOCOL_VERSION_MIN_SUPPORTED,
    __version__,
)
from app.auth import validate_pi_token
from app.config import get_settings
from app.handlers import register_handlers
from app.marketing import STATIC_DIR, build_marketing_router
from app.routes import build_router
from app.state import MeetStateStore
from app.telemetry import configure_telemetry
from app.watchdog import MeetWatchdog


def build_app(
    *,
    redis_client=None,
    token_validator=None,
) -> tuple[FastAPI, socketio.AsyncServer, Any]:
    """Construct the FastAPI app, Socket.IO server, and ASGI composite.

    ``redis_client`` and ``token_validator`` are overridable so tests can
    inject ``fakeredis.FakeRedis`` and stub auth.
    """
    settings = get_settings()
    configure_telemetry(
        connection_string=settings.applicationinsights_connection_string,
        environment=settings.environment,
    )
    redis_handle = redis_client or redis_sync.from_url(
        settings.redis_url,
        decode_responses=False,
        health_check_interval=30,
        socket_keepalive=True,
        socket_timeout=60,
        socket_connect_timeout=10,
    )
    store = MeetStateStore(redis_handle)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.redis = redis_handle
        app.state.store = store
        yield

    fastapi_app = FastAPI(
        title="Swimming Scoreboard Azure Relay",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    # Explicitly instrument FastAPI for Application Insights. The
    # azure-monitor-opentelemetry distro is supposed to pick this up
    # automatically, but in practice the auto-discovery silently no-ops
    # in some build/import orderings and `requests` ends up empty in AI
    # even though the app is serving traffic. Calling
    # `instrument_app` here is idempotent and guarantees the
    # ASGI middleware is wired before `socketio.ASGIApp` wraps us.
    if settings.applicationinsights_connection_string:
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            FastAPIInstrumentor.instrument_app(fastapi_app)
        except Exception:  # pragma: no cover - best-effort
            import logging

            logging.exception("FastAPI OTel instrumentation failed")

    # Cross-process / cross-replica fanout for Socket.IO.
    #
    # Each engine.io session lives in one worker process — so an emit() to
    # a room from worker A won't reach a client connected to worker B
    # unless the two workers share a pub/sub channel. AsyncRedisManager
    # provides exactly that on top of the same Redis we already use for
    # meet state. With it in place we can run multiple gunicorn workers
    # per replica AND multiple Container Apps replicas; without it we'd
    # silently drop fanout to all-but-one of them.
    #
    # In tests, callers pass redis_client=fakeredis. Skip the manager
    # there because AsyncRedisManager opens its own real connection from
    # settings.redis_url that fakeredis can't intercept.
    client_manager = None
    if redis_client is None:
        # Azure Container Apps' load balancer silently drops idle TCP
        # connections after a few minutes. The Socket.IO pubsub subscriber
        # blocks indefinitely with no traffic, so without health checks the
        # connection gets reset and we see "Cannot receive from redis...
        # retrying in 1 secs" on every cycle. health_check_interval makes
        # the redis client send PING every 30s, keeping the connection alive.
        client_manager = socketio.AsyncRedisManager(
            settings.redis_url,
            redis_options={
                "health_check_interval": 30,
                "socket_keepalive": True,
                "socket_timeout": 60,
                "socket_connect_timeout": 10,
            },
        )

    sio: socketio.AsyncServer = socketio.AsyncServer(
        async_mode="asgi",
        cors_allowed_origins="*",
        client_manager=client_manager,
    )

    register_handlers(
        sio,
        store=store,
        tenant_id=settings.entra_tenant_id,
        audience=settings.entra_audience,
        token_validator=token_validator,
    )

    watchdog = MeetWatchdog(
        store=store,
        emitter=sio.emit,
        degraded_after_s=settings.heartbeat_degraded_seconds,
        close_after_s=settings.heartbeat_close_seconds,
    )

    @asynccontextmanager
    async def lifespan_with_watchdog(app: FastAPI):
        async with lifespan(app):
            watchdog.start()
            try:
                yield
            finally:
                await watchdog.stop()

    fastapi_app.router.lifespan_context = lifespan_with_watchdog
    fastapi_app.include_router(
        build_router(
            store=store,
            token_validator=(
                token_validator
                or (
                    lambda t: validate_pi_token(
                        t,
                        tenant_id=settings.entra_tenant_id,
                        audience=settings.entra_audience,
                    )
                )
            ),
        )
    )

    # Public landing pages (/, /terms, /privacy) live under their own
    # router to keep marketing concerns separate from the per-meet API.
    fastapi_app.include_router(build_marketing_router())

    # Static assets backing the marketing pages (CSS + the QR demo SVG).
    # NOTE: this is distinct from the per-meet /m/{meet_id}/static/... route,
    # which serves Pi-bundled template assets out of Redis. The two surfaces
    # don't overlap by path.
    fastapi_app.mount(
        "/static",
        StaticFiles(directory=str(STATIC_DIR)),
        name="static",
    )

    @fastapi_app.get("/healthz", response_class=PlainTextResponse, include_in_schema=False)
    async def healthz() -> str:
        return "ok"

    @fastapi_app.get("/readyz", response_class=JSONResponse, include_in_schema=False)
    async def readyz() -> JSONResponse:
        return JSONResponse({"status": "ready", "version": __version__})

    @fastapi_app.get("/version", response_class=JSONResponse, include_in_schema=False)
    async def version() -> JSONResponse:
        return JSONResponse(
            {
                "app_version": __version__,
                "protocol_version_current": PROTOCOL_VERSION_CURRENT,
                "protocol_version_min_supported": PROTOCOL_VERSION_MIN_SUPPORTED,
                "environment": settings.environment,
            }
        )

    asgi = socketio.ASGIApp(sio, other_asgi_app=fastapi_app, socketio_path="socket.io")
    return fastapi_app, sio, asgi


fastapi_app, sio, asgi_app = build_app()
