"""FastAPI + Socket.IO entrypoint for the relay app."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import redis as redis_sync
import socketio
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse

from app import (
    PROTOCOL_VERSION_CURRENT,
    PROTOCOL_VERSION_MIN_SUPPORTED,
    __version__,
)
from app.config import get_settings
from app.handlers import register_handlers
from app.routes import build_router
from app.state import MeetStateStore
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
    redis_handle = redis_client or redis_sync.from_url(settings.redis_url, decode_responses=False)
    store = MeetStateStore(redis_handle)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.redis = redis_handle
        app.state.store = store
        yield

    fastapi_app = FastAPI(
        title="CTS Scoreboard Azure Relay",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    sio: socketio.AsyncServer = socketio.AsyncServer(
        async_mode="asgi",
        cors_allowed_origins="*",
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
    fastapi_app.include_router(build_router(store=store))

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
