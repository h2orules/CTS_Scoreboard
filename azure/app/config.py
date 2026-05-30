"""Environment-driven configuration for the relay app.

All secrets and Azure resource connection strings are sourced from environment
variables (set by Container Apps from Key Vault refs or plain env). Nothing is
hard-coded.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Loaded once and cached."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- environment ----
    # Accept both "production" and the short "prod" form used by the Bicep
    # infra (environmentName='prod'), so the same image works in either.
    environment: Literal["preprod", "prod", "production", "local"] = Field(
        default="local",
        description="Deployment environment label, surfaced in telemetry.",
    )
    log_level: str = Field(default="INFO")

    # ---- state stores ----
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Azure Cache for Redis connection URL (rediss:// in prod).",
    )
    storage_connection_string: str = Field(
        default="",
        description="Azure Storage connection string for Tables + Blob.",
    )
    storage_meets_table: str = Field(default="meets")
    storage_snapshots_container: str = Field(default="meet-snapshots")

    # ---- Pi authentication via Entra ID ----
    entra_tenant_id: str = Field(default="", description="Entra tenant ID for Pi auth.")
    entra_audience: str = Field(
        default="",
        description="Application (client) ID of the relay app registration; required audience for Pi tokens.",
    )

    # ---- protocol versioning ----
    # Mirrored from app/__init__.py; overridable via env for ad-hoc rollback.
    protocol_version_current: int = Field(default=1)
    protocol_version_min_supported: int = Field(default=1)

    # ---- meet lifecycle ----
    meet_id_length: int = Field(default=15, ge=8, le=32)
    heartbeat_degraded_seconds: int = Field(default=60)
    heartbeat_close_seconds: int = Field(default=8 * 3600)

    # ---- relay performance ----
    # Per-meet coalescing window for high-frequency Pi events. Incoming
    # update_scoreboard / event_info / scores_info / message_overlay_state
    # payloads merge into a pending buffer and flush once per window,
    # capping Redis writes and Socket.IO fan-out rate. 0 disables.
    coalesce_window_seconds: float = Field(default=0.1, ge=0.0, le=2.0)

    # Per-replica read caches for HTML fragments and template bundles.
    # 0 disables the respective cache. Template bundles and fragments are
    # both immutable per content-addressed key, so a long TTL backstop is
    # safe — the primary bound for fragments is max_entries (FIFO).
    # current_template uses a short TTL because the active bundle pointer
    # can change at any time and we tolerate brief staleness for read relief.
    fragment_cache_ttl_seconds: float = Field(default=3600.0, ge=0.0, le=86400.0)
    fragment_cache_max_entries: int = Field(default=1024, ge=0, le=16384)
    current_template_cache_ttl_seconds: float = Field(default=2.0, ge=0.0, le=60.0)
    template_blob_cache_max: int = Field(default=16, ge=0, le=512)

    # ---- telemetry ----
    applicationinsights_connection_string: str = Field(default="")
    # How often to poll Redis ``INFO`` for memory / clients / ops/sec stats
    # that feed observable gauges. 0 disables the poller entirely.
    redis_info_scrape_seconds: float = Field(default=30.0, ge=0.0, le=300.0)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
