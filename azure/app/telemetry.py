"""OpenTelemetry / Application Insights setup.

Phase 1: scaffold. Phase 7 wires up:
  - azure.monitor.opentelemetry.configure_azure_monitor() with the
    APPLICATIONINSIGHTS_CONNECTION_STRING.
  - Custom metrics: meets_active, clients_active, client_connects_total,
    client_errors_total, pi_reconnects_total, pi_disconnects_total,
    template_pushes_total, pi_connected_seconds_total,
    pi_degraded_seconds_total.
  - Structured logs via structlog routed to OTLP.
"""
from __future__ import annotations

import logging
from typing import Final

logger: Final = logging.getLogger("cts.relay")


def init_telemetry(connection_string: str | None) -> None:
    """Initialise telemetry. No-op when connection string is empty."""
    if not connection_string:
        logger.info("telemetry disabled (no APPLICATIONINSIGHTS_CONNECTION_STRING)")
        return
    # TODO(phase-7): configure_azure_monitor(connection_string=connection_string)
    logger.info("telemetry placeholder configured")
