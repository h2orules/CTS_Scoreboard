"""End-to-end test that runs only when ``RELAY_E2E_URL`` is set.

In CI we leave this skipped. Run it manually after a pre-prod deploy:

    RELAY_E2E_URL=https://<preprod-fqdn> pytest tests/e2e -v

It verifies the public health probes and confirms the unknown-meet path
returns the friendly 404 page (not a stack trace).
"""
from __future__ import annotations

import os

import httpx
import pytest

E2E_URL = os.environ.get("RELAY_E2E_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(
    not E2E_URL,
    reason="RELAY_E2E_URL not set; deployment E2E skipped",
)


def test_healthz_reachable() -> None:
    res = httpx.get(f"{E2E_URL}/healthz", timeout=10.0)
    assert res.status_code == 200
    assert res.text.strip() == "ok"


def test_readyz_reachable() -> None:
    res = httpx.get(f"{E2E_URL}/readyz", timeout=10.0)
    assert res.status_code == 200
    body = res.json()
    assert body.get("status") == "ready"


def test_version_advertises_protocol() -> None:
    res = httpx.get(f"{E2E_URL}/version", timeout=10.0)
    assert res.status_code == 200
    body = res.json()
    assert "app_version" in body
    assert body.get("protocol_version_current", 0) >= 1


def test_unknown_meet_returns_friendly_404() -> None:
    res = httpx.get(f"{E2E_URL}/m/zzz-not-a-meet-zzz", timeout=10.0)
    assert res.status_code == 404
    # Should be the rendered "no meet found" page, not a JSON error.
    assert "<html" in res.text.lower()
