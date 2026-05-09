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
    environment: Literal["preprod", "production", "local"] = Field(
        default="local",
        description="Deployment environment label, surfaced in telemetry.",
    )
    log_level: str = Field(default="INFO")

    # ---- realtime transport (Azure Web PubSub for Socket.IO) ----
    webpubsub_connection_string: str = Field(
        default="",
        description="Connection string for the Web PubSub for Socket.IO resource.",
    )
    webpubsub_hub: str = Field(
        default="scoreboard",
        description="Web PubSub hub name; defaults to 'scoreboard'.",
    )

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

    # ---- telemetry ----
    applicationinsights_connection_string: str = Field(default="")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
