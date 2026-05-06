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
from dataclasses import dataclass
from typing import Any

import structlog

# Module-level guards so we don't double-init on test reloads.
_configured: bool = False
_metrics: Metrics | None = None


@dataclass
class Metrics:
    """Thin wrapper around OpenTelemetry counters/gauges.

    Falls back to in-memory accumulators when OpenTelemetry isn't available
    (e.g. local dev without an App Insights connection string).
    """

    meet_opened: Any
    meet_closed: Any
    meet_degraded: Any
    browser_connected: Any
    browser_disconnected: Any
    relay_event_processed: Any

    @classmethod
    def stub(cls) -> Metrics:
        return cls(
            meet_opened=_StubCounter(),
            meet_closed=_StubCounter(),
            meet_degraded=_StubCounter(),
            browser_connected=_StubCounter(),
            browser_disconnected=_StubCounter(),
            relay_event_processed=_StubCounter(),
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

            os.environ.setdefault(
                "APPLICATIONINSIGHTS_CONNECTION_STRING", connection_string
            )
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
