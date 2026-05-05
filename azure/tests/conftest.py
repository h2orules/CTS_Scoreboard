"""Shared pytest fixtures for the Azure relay tests."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import fastapi_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(fastapi_app)
