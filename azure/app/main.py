"""FastAPI + Socket.IO entrypoint for the relay app.

This module wires the FastAPI HTTP app together with the Socket.IO ASGI app
and exposes ``asgi_app`` as the gunicorn target.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import socketio
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse

from app import (
    PROTOCOL_VERSION_CURRENT,
    PROTOCOL_VERSION_MIN_SUPPORTED,
    __version__,
)
from app.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle. Phase 1 scaffold: nothing to wire yet."""
    settings = get_settings()
    app.state.settings = settings
    yield


fastapi_app = FastAPI(
    title="CTS Scoreboard Azure Relay",
    version=__version__,
    docs_url=None,  # Disable public API docs by default; re-enable per environment if desired.
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


# ---------- Socket.IO server ----------
# Async Socket.IO server. In production this is fronted by Azure Web PubSub
# for Socket.IO via the published adapter; in local/test mode it runs in-process.
sio: socketio.AsyncServer = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*",  # Browser access is anonymous; meet ID is the token.
)


@sio.event(namespace="/scoreboard")
async def connect(sid: str, environ: dict[str, Any], auth: dict[str, Any] | None = None) -> None:
    """Browser-facing namespace. Phase 1 scaffold: accept all connects."""
    return None


@sio.event(namespace="/scoreboard")
async def disconnect(sid: str) -> None:
    return None


# ---------- HTTP endpoints ----------
@fastapi_app.get("/healthz", response_class=PlainTextResponse, include_in_schema=False)
async def healthz() -> str:
    """Liveness probe. Returns 200 if the process is up."""
    return "ok"


@fastapi_app.get("/readyz", response_class=JSONResponse, include_in_schema=False)
async def readyz() -> JSONResponse:
    """Readiness probe. Phase 1 scaffold: always ready; later phases check
    Redis/Storage/Web PubSub connectivity."""
    return JSONResponse({"status": "ready", "version": __version__})


@fastapi_app.get("/version", response_class=JSONResponse, include_in_schema=False)
async def version() -> JSONResponse:
    settings = get_settings()
    return JSONResponse(
        {
            "app_version": __version__,
            "protocol_version_current": PROTOCOL_VERSION_CURRENT,
            "protocol_version_min_supported": PROTOCOL_VERSION_MIN_SUPPORTED,
            "environment": settings.environment,
        }
    )


# ---------- ASGI composition ----------
# Mount the Socket.IO ASGI app at root, with FastAPI handling everything else.
asgi_app = socketio.ASGIApp(sio, other_asgi_app=fastapi_app, socketio_path="socket.io")
