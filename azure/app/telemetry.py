"""Telemetry wiring for the relay (Phase 7).

Two pieces, both opt-in:

- **Structlog** is configured to emit JSON in production-style environments
  and a friendly key=value format locally. Always safe to call.
- **Azure Monitor OpenTelemetry** auto-instrumentation is set up only when an
  Application Insights connection string is provided. Custom metrics are
  registered against the global meter so callers can record them via simple
  helpers.

Calling :func:`configure_telemetry` more than once is a no-op (idempotent).
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import structlog

# Module-level guards so we don't double-init on test reloads.
_configured: bool = False
_metrics: Metrics | None = None


@dataclass
class Metrics:
    """Thin wrapper around OpenTelemetry counters/histograms/gauges.

    Falls back to in-memory accumulators when OpenTelemetry isn't available
    (e.g. local dev without an App Insights connection string).
    """

    meet_opened: Any
    meet_closed: Any
    meet_degraded: Any
    browser_connected: Any
    browser_disconnected: Any
    relay_event_processed: Any
    event_handler_seconds: Any
    redis_op_seconds: Any
    emit_fanout_seconds: Any
    active_sockets: Any
    pi_connections: Any
    cache_hits: Any
    cache_misses: Any
    coalescer_events_in: Any
    coalescer_batches_flushed: Any
    coalescer_batch_size: Any

    @classmethod
    def stub(cls) -> Metrics:
        return cls(
            meet_opened=_StubCounter(),
            meet_closed=_StubCounter(),
            meet_degraded=_StubCounter(),
            browser_connected=_StubCounter(),
            browser_disconnected=_StubCounter(),
            relay_event_processed=_StubCounter(),
            event_handler_seconds=_StubHistogram(),
            redis_op_seconds=_StubHistogram(),
            emit_fanout_seconds=_StubHistogram(),
            active_sockets=_StubUpDownCounter(),
            pi_connections=_StubUpDownCounter(),
            cache_hits=_StubCounter(),
            cache_misses=_StubCounter(),
            coalescer_events_in=_StubCounter(),
            coalescer_batches_flushed=_StubCounter(),
            coalescer_batch_size=_StubHistogram(),
        )


class _StubCounter:
    """Drop-in for opentelemetry Counter when telemetry is disabled.

    Records the running total so tests can assert on it; mimics the OTel
    Counter API shape (``add(value, attributes={...})``).
    """

    def __init__(self) -> None:
        self.total: float = 0.0
        self.events: list[tuple[float, dict[str, Any] | None]] = []

    def add(self, value: float, attributes: dict[str, Any] | None = None) -> None:
        self.total += value
        self.events.append((value, dict(attributes) if attributes else None))


class _StubHistogram:
    """Drop-in for opentelemetry Histogram when telemetry is disabled.

    Mimics the OTel Histogram API shape (``record(value, attributes={...})``).
    Stores observations so tests can assert on them.
    """

    def __init__(self) -> None:
        self.events: list[tuple[float, dict[str, Any] | None]] = []

    def record(self, value: float, attributes: dict[str, Any] | None = None) -> None:
        self.events.append((value, dict(attributes) if attributes else None))


class _StubUpDownCounter:
    """Drop-in for opentelemetry UpDownCounter when telemetry is disabled.

    Tracks running total (which can go negative) so tests can assert on the
    current value as well as individual deltas.
    """

    def __init__(self) -> None:
        self.total: float = 0.0
        self.events: list[tuple[float, dict[str, Any] | None]] = []

    def add(self, value: float, attributes: dict[str, Any] | None = None) -> None:
        self.total += value
        self.events.append((value, dict(attributes) if attributes else None))


@contextmanager
def record_latency(histogram: Any, attributes: dict[str, Any] | None = None) -> Iterator[None]:
    """Time the wrapped block and record the elapsed seconds on ``histogram``.

    Works with both the real OTel ``Histogram`` and the local stub. The
    timing spans wall-clock from ``__enter__`` to ``__exit__``, including
    any ``await`` points inside an ``async with`` would, but this is a sync
    context manager so the event loop is not suspended on its behalf.
    """
    t0 = time.perf_counter()
    try:
        yield
    finally:
        histogram.record(time.perf_counter() - t0, attributes or {})


def configure_telemetry(
    *,
    connection_string: str = "",
    environment: str = "local",
    json_logs: bool | None = None,
) -> Metrics:
    """Initialize logging + (optionally) Azure Monitor.

    Returns the :class:`Metrics` registry. Always returns a usable object,
    even when no connection string is configured.
    """
    global _configured, _metrics
    if _configured and _metrics is not None:
        return _metrics

    use_json = json_logs if json_logs is not None else environment != "local"
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    if use_json:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=False))
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    metrics: Metrics
    if connection_string:
        # Defer the heavy Azure imports so the process boots fast without
        # them and tests don't pay the cost.
        try:
            from azure.monitor.opentelemetry import configure_azure_monitor
            from opentelemetry import metrics as ot_metrics

            os.environ.setdefault("APPLICATIONINSIGHTS_CONNECTION_STRING", connection_string)
            configure_azure_monitor(
                connection_string=connection_string,
                resource_attributes={
                    "service.namespace": "cts-scoreboard",
                    "deployment.environment": environment,
                },
            )
            meter = ot_metrics.get_meter("cts-scoreboard.relay")
            metrics = Metrics(
                meet_opened=meter.create_counter("meets_opened"),
                meet_closed=meter.create_counter("meets_closed"),
                meet_degraded=meter.create_counter("meets_degraded"),
                browser_connected=meter.create_counter("browsers_connected"),
                browser_disconnected=meter.create_counter("browsers_disconnected"),
                relay_event_processed=meter.create_counter("relay_events_processed"),
                event_handler_seconds=meter.create_histogram("event_handler_seconds", unit="s"),
                redis_op_seconds=meter.create_histogram("redis_op_seconds", unit="s"),
                emit_fanout_seconds=meter.create_histogram("emit_fanout_seconds", unit="s"),
                active_sockets=meter.create_up_down_counter("active_sockets"),
                pi_connections=meter.create_up_down_counter("pi_connections"),
                cache_hits=meter.create_counter("cache_hits"),
                cache_misses=meter.create_counter("cache_misses"),
                coalescer_events_in=meter.create_counter("coalescer_events_in"),
                coalescer_batches_flushed=meter.create_counter("coalescer_batches_flushed"),
                coalescer_batch_size=meter.create_histogram(
                    "coalescer_batch_size",
                    unit="{events}",
                    description="Number of Pi events merged into one coalesced flush.",
                ),
            )
        except Exception:  # pragma: no cover - log + fall back to stub
            logging.exception("azure-monitor init failed; using stub metrics")
            metrics = Metrics.stub()
    else:
        metrics = Metrics.stub()

    _metrics = metrics
    _configured = True
    return metrics


def get_metrics() -> Metrics:
    """Return the registered metrics, configuring stubs lazily if needed."""
    global _metrics
    if _metrics is None:
        return configure_telemetry()
    return _metrics


def reset_for_tests() -> None:
    """Test helper: clear the singleton so the next configure call applies."""
    global _configured, _metrics
    _configured = False
    _metrics = None


# Module-level cache for the viewer-event logger so we don't reconfigure on
# every POST. The logger name is the conventional "customEvents" bucket in
# App Insights when the OTel logging exporter is in use.
_viewer_logger: logging.Logger | None = None


def _viewer_event_logger() -> logging.Logger:
    global _viewer_logger
    if _viewer_logger is None:
        _viewer_logger = logging.getLogger("cts.viewer")
        # The root logger's level defaults to WARNING (configure_azure_monitor
        # does not change it).  Child loggers inherit the effective level from
        # the root unless they have their own level set, so logger.info() calls
        # are silently dropped before a LogRecord is even created.  Setting
        # INFO here is scoped to this namespace only — other library loggers
        # remain at WARNING and won't flood App Insights.
        _viewer_logger.setLevel(logging.INFO)
        # Propagate to root so the OTel LoggingHandler (level=NOTSET) exports
        # these records to App Insights traces.
        _viewer_logger.propagate = True
    return _viewer_logger


def emit_viewer_event(name: str, props: dict[str, Any]) -> None:
    """Forward a single viewer-engagement event to App Insights.

    Implemented as a structured log record so it lands in ``traces`` /
    ``customEvents`` via the existing OTel logging exporter that
    ``configure_azure_monitor`` installs. No-op safe when telemetry is the
    stub: the record still hits the local logger but does not leave the box.
    """
    logger = _viewer_event_logger()
    # ``extra`` keys become OTel attributes on the log record, which the
    # Azure Monitor exporter promotes to customDimensions.
    safe_props: dict[str, Any] = {}
    for k, v in props.items():
        if isinstance(k, str) and isinstance(v, (str, int, float, bool)):
            safe_props[k] = v
    logger.info(name, extra={"viewer_event": name, **safe_props})
