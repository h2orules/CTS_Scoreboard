"""Sanity tests for the FastAPI HTTP surface."""
from __future__ import annotations

from unittest.mock import AsyncMock

import fakeredis.aioredis
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError

from app import (
    PROTOCOL_VERSION_CURRENT,
    PROTOCOL_VERSION_MIN_SUPPORTED,
    __version__,
)
from app.main import build_app


def test_healthz_returns_ok(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.text == "ok"


def test_readyz_returns_ready(client: TestClient) -> None:
    resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["version"] == __version__


def test_readyz_fails_when_redis_is_unreachable() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    redis.execute_command = AsyncMock(side_effect=ConnectionError("unreachable"))
    app, _, _ = build_app(redis_client=redis)
    with TestClient(app) as client:
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable", "dependency": "redis"}
        assert client.get("/healthz").status_code == 200


def test_version_endpoint_includes_protocol(client: TestClient) -> None:
    resp = client.get("/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["app_version"] == __version__
    assert body["protocol_version_current"] == PROTOCOL_VERSION_CURRENT
    assert body["protocol_version_min_supported"] == PROTOCOL_VERSION_MIN_SUPPORTED
    assert "environment" in body


def test_docs_disabled_by_default(client: TestClient) -> None:
    """API docs are off so we don't accidentally expose internals."""
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404
