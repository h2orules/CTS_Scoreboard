"""Shared pytest fixtures for the Azure relay tests."""
from __future__ import annotations

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from app.main import build_app


@pytest.fixture
def client() -> TestClient:
    fastapi_app, _, _ = build_app(redis_client=fakeredis.aioredis.FakeRedis())
    return TestClient(fastapi_app)
