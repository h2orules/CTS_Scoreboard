"""Tests for app.telemetry."""
from __future__ import annotations

import json

import pytest
import structlog

from app.telemetry import (
    Metrics,
    _StubCounter,
    _StubHistogram,
    _StubUpDownCounter,
    configure_telemetry,
    get_metrics,
    record_latency,
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


def test_stub_histogram_records_observations():
    h = _StubHistogram()
    h.record(0.001, {"op": "put_state"})
    h.record(0.002)
    assert h.events[0] == (0.001, {"op": "put_state"})
    assert h.events[1] == (0.002, None)


def test_stub_updown_counter_tracks_running_total():
    g = _StubUpDownCounter()
    g.add(1, {"namespace": "/scoreboard"})
    g.add(1, {"namespace": "/scoreboard"})
    g.add(-1, {"namespace": "/scoreboard"})
    assert g.total == 1
    assert len(g.events) == 3


def test_configure_registers_new_metric_fields():
    m = configure_telemetry(connection_string="", environment="local")
    assert isinstance(m.event_handler_seconds, _StubHistogram)
    assert isinstance(m.redis_op_seconds, _StubHistogram)
    assert isinstance(m.emit_fanout_seconds, _StubHistogram)
    assert isinstance(m.active_sockets, _StubUpDownCounter)
    assert isinstance(m.pi_connections, _StubUpDownCounter)
    assert isinstance(m.cache_hits, _StubCounter)
    assert isinstance(m.cache_misses, _StubCounter)
    assert isinstance(m.coalescer_events_in, _StubCounter)
    assert isinstance(m.coalescer_batches_flushed, _StubCounter)
    assert isinstance(m.coalescer_batch_size, _StubHistogram)


def test_record_latency_writes_one_observation():
    h = _StubHistogram()
    with record_latency(h, {"op": "x"}):
        pass
    assert len(h.events) == 1
    elapsed, attrs = h.events[0]
    assert elapsed >= 0
    assert attrs == {"op": "x"}


def test_record_latency_records_even_on_exception():
    h = _StubHistogram()
    with pytest.raises(ValueError), record_latency(h, {"op": "x"}):
        raise ValueError("boom")
    assert len(h.events) == 1
