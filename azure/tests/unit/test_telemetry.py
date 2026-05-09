"""Tests for app.telemetry."""
from __future__ import annotations

import json

import pytest
import structlog

from app.telemetry import (
    Metrics,
    _StubCounter,
    configure_telemetry,
    get_metrics,
    reset_for_tests,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_for_tests()
    yield
    reset_for_tests()


def test_configure_without_connection_string_returns_stub_metrics():
    m = configure_telemetry(connection_string="", environment="local")
    assert isinstance(m, Metrics)
    assert isinstance(m.meet_opened, _StubCounter)


def test_stub_counter_records_adds():
    c = _StubCounter()
    c.add(1, {"k": "v"})
    c.add(2)
    assert c.total == 3
    assert c.events[0] == (1, {"k": "v"})
    assert c.events[1] == (2, None)


def test_configure_is_idempotent():
    m1 = configure_telemetry(connection_string="", environment="local")
    m2 = configure_telemetry(connection_string="", environment="local")
    # Same singleton on second call.
    assert m1 is m2


def test_get_metrics_lazy_inits():
    m = get_metrics()
    assert isinstance(m, Metrics)


def test_structlog_emits_json_when_environment_not_local(capsys):
    configure_telemetry(connection_string="", environment="preprod")
    log = structlog.get_logger()
    log.info("hello", meet_id="abc")
    captured = capsys.readouterr().out.strip().splitlines()
    # JSONRenderer always emits a parseable JSON object.
    assert captured
    last = json.loads(captured[-1])
    assert last["event"] == "hello"
    assert last["meet_id"] == "abc"
    assert last["level"] == "info"


def test_structlog_friendly_in_local_mode(capsys):
    configure_telemetry(connection_string="", environment="local")
    log = structlog.get_logger()
    log.info("hello", meet_id="abc")
    out = capsys.readouterr().out
    # Console renderer is not JSON, but key=value style.
    assert "meet_id" in out and "abc" in out
