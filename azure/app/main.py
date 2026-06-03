"""FastAPI + Socket.IO entrypoint for the relay app."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import orjson
import redis.asyncio as redis_async
import socketio
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
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


class _OrjsonForSocketIO:
    """Adapter so python-socketio can use orjson as its serializer.

    socketio calls ``self.json.dumps(...)`` expecting a ``str`` return; orjson
    returns ``bytes`` and rejects the ``separators=`` kwarg socketio passes
    for compact encoding. The shim handles both. Net effect: every Socket.IO
    packet serialization (including every ``await sio.emit(...)``) skips the
    stdlib ``json`` codepath, which the stress test showed dominates CPU on
    the worker that holds the Pi connection.
    """

    @staticmethod
    def dumps(obj: Any, **_kwargs: Any) -> str:
        return orjson.dumps(obj).decode("utf-8")

    @staticmethod
    def loads(s: Any, **_kwargs: Any) -> Any:
        return orjson.loads(s)


def _build_state_redis(url: str) -> redis_async.Redis:
    """Build the async Redis client used by MeetStateStore.

    Uses a BlockingConnectionPool so request coroutines queue (with a short
    timeout) for a connection instead of erroring with
    ``ConnectionError: Too many connections`` the moment the pool saturates.
    The per-worker cap is intentionally small so the total connection count
    across workers x replicas stays well under the Azure Cache for Redis
    Basic C0 ceiling (256 conns) even at our current ``maxReplicas`` of 20:
    10 conns/worker x 2 workers x 20 replicas = 400, plus the Socket.IO
    pub/sub manager's connections. If real load consistently queues here,
    bump the Redis SKU (C1 = 1000 conns) rather than this cap.
    """
    pool = redis_async.BlockingConnectionPool.from_url(
        url,
        decode_responses=False,
        max_connections=10,
        timeout=5,
        health_check_interval=30,
        socket_keepalive=True,
        socket_timeout=5,
        socket_connect_timeout=5,
    )
    return redis_async.Redis(connection_pool=pool)


def build_app(
    *,
    redis_client: Any = None,
    token_validator: Any = None,
) -> tuple[FastAPI, socketio.AsyncServer, Any]:
    """Construct the FastAPI app, Socket.IO server, and ASGI composite.

    ``redis_client`` and ``token_validator`` are overridable so tests can
    inject ``fakeredis.aioredis.FakeRedis`` and stub auth.
    """
    settings = get_settings()
    configure_telemetry(
        connection_string=settings.applicationinsights_connection_string,
        environment=settings.environment,
    )
    redis_handle = redis_client or _build_state_redis(settings.redis_url)
    store = MeetStateStore(
        redis_handle,
        fragment_cache_ttl=settings.fragment_cache_ttl_seconds,
        fragment_cache_max_entries=settings.fragment_cache_max_entries,
        current_template_cache_ttl=settings.current_template_cache_ttl_seconds,
        template_blob_cache_max=settings.template_blob_cache_max,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.redis = redis_handle
        app.state.store = store
        scale_tel = None
        if settings.redis_info_scrape_seconds > 0:
            from app.scale_telemetry import ScaleTelemetry

            scale_tel = ScaleTelemetry(
                store=store,
                redis=redis_handle,
                poll_interval_s=settings.redis_info_scrape_seconds,
            )
            scale_tel.start()
            app.state.scale_telemetry = scale_tel
        try:
            yield
        finally:
            if scale_tel is not None:
                await scale_tel.stop()
            # Close the async Redis pool on shutdown so connections aren't
            # leaked between hot reloads in development. No-op on fakeredis.
            close = getattr(redis_handle, "aclose", None) or getattr(redis_handle, "close", None)
            if close is not None:
                try:
                    result = close()
                    if hasattr(result, "__await__"):
                        await result
                except Exception:  # pragma: no cover - best-effort
                    pass

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
        # connections after a few minutes. health_check_interval keeps the
        # pub/sub subscriber connection alive with a PING every 30s.
        # socket_timeout must NOT be set here: the pub/sub subscriber blocks
        # indefinitely waiting for messages, and a short timeout causes
        # "Cannot receive from redis... retrying in 1 secs" every few seconds.
        client_manager = socketio.AsyncRedisManager(
            settings.redis_url,
            redis_options={
                # health_check_interval keeps the idle pub/sub connection
                # alive through Azure's TCP idle-timeout.  Do NOT set
                # socket_timeout here: the pub/sub subscriber blocks
                # indefinitely waiting for messages, so a short socket
                # timeout fires every few seconds and produces the noisy
                # "Cannot receive from redis... retrying in 1 secs" log.
                "health_check_interval": 30,
                "socket_keepalive": True,
                "socket_connect_timeout": 5,
            },
        )

    sio: socketio.AsyncServer = socketio.AsyncServer(
        async_mode="asgi",
        cors_allowed_origins="*",
        client_manager=client_manager,
        json=_OrjsonForSocketIO,
        # Engine.IO heartbeat. Default ping_timeout=20s drops viewers
        # whenever a replica's asyncio loop is momentarily saturated
        # (the hot-replica failure mode under stress). 60s gives the
        # coalescer time to drain backlog without killing live connections.
        ping_interval=25,
        ping_timeout=60,
    )

    register_handlers(
        sio,
        store=store,
        tenant_id=settings.entra_tenant_id,
        audience=settings.entra_audience,
        token_validator=token_validator,
        coalesce_window_s=settings.coalesce_window_seconds,
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

    @fastapi_app.get("/favicon.ico", include_in_schema=False)
    async def favicon_ico() -> FileResponse:
        # Browsers request /favicon.ico by default. Serve the SVG with the
        # correct MIME type; modern browsers honor the content type rather
        # than the URL suffix.
        return FileResponse(
            str(STATIC_DIR / "favicon.svg"),
            media_type="image/svg+xml",
        )

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
