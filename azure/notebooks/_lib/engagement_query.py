"""Shared KQL + query helpers for the engagement notebooks.

All notebooks should import from here so the session-stitching logic is
defined exactly once: a viewing session is a run of ``viewer_heartbeat`` /
``viewer_event_view`` rows for a single ``(meet_id, pi_local_date,
viewer_id)`` triple where successive timestamps are <= 5 minutes apart.

Authentication uses :class:`DefaultAzureCredential` so the same notebook
works locally (``az login``) and in a managed identity context.
"""
from __future__ import annotations

import os
from datetime import timedelta
from typing import Any

import pandas as pd
from azure.identity import DefaultAzureCredential
from azure.monitor.query import LogsQueryClient, LogsQueryStatus

# All engagement events go through customDimensions.viewer_event so the
# core scoreboard logs aren't mixed in. Each row is a single emit from
# engagement.js (page_load, heartbeat, event_view, hidden, visible, unload).
BASE_PROJECT = """
| where customDimensions has 'viewer_event'
| extend ev = tostring(customDimensions.viewer_event)
| extend meet_id = tostring(customDimensions.meet_id),
         pi_local_date = tostring(customDimensions.pi_local_date),
         viewer_id = tostring(customDimensions.viewer_id),
         device_hash = tostring(customDimensions.device_hash),
         current_event = tostring(customDimensions.current_event),
         current_heat = tostring(customDimensions.current_heat),
         distance = toint(customDimensions.distance),
         stroke_name = tostring(customDimensions.stroke_name),
         age_group = tostring(customDimensions.age_group),
         gender = tostring(customDimensions.gender),
         relay = tobool(customDimensions.relay),
         tenure_ms = tolong(customDimensions.tenure_ms),
         lcp_ms = tolong(customDimensions.lcp_ms),
         effective_type = tostring(customDimensions.effective_type)
"""

# Shared session-stitching KQL: 5-min gap rule. Caller must already have
# filtered by meet_id / pi_local_date and projected the base columns.
SESSION_STITCH = """
| sort by viewer_id asc, timestamp asc
| serialize
| extend prev_ts = prev(timestamp), prev_viewer = prev(viewer_id)
| extend new_session = iff(viewer_id != prev_viewer or
                            datetime_diff('second', timestamp, prev_ts) > 300, 1, 0)
| extend session_idx = row_cumsum(new_session)
"""


def _client() -> LogsQueryClient:
    return LogsQueryClient(DefaultAzureCredential())


def run_kql(workspace_id: str, query: str, *, days: int = 14) -> pd.DataFrame:
    """Run a KQL query against Log Analytics and return a DataFrame."""
    client = _client()
    resp = client.query_workspace(
        workspace_id=workspace_id,
        query=query,
        timespan=timedelta(days=days),
    )
    if resp.status != LogsQueryStatus.SUCCESS:  # type: ignore[attr-defined]
        raise RuntimeError(f"KQL query failed: {resp.partial_error or resp}")
    table = resp.tables[0]
    return pd.DataFrame(data=table.rows, columns=[c for c in table.columns])


def per_meet_query(meet_id: str, pi_local_date: str) -> str:
    """Build the canonical per-meet engagement query for a single meet day."""
    return f"""
traces
{BASE_PROJECT}
| where meet_id == '{meet_id}' and pi_local_date == '{pi_local_date}'
{SESSION_STITCH}
"""


def cross_meet_query(*, days: int = 30) -> str:
    """Cross-meet rollup: one row per (meet_id, pi_local_date)."""
    return f"""
traces
{BASE_PROJECT}
| where isnotempty(meet_id) and isnotempty(pi_local_date)
| summarize
    unique_viewers = dcount(viewer_id),
    unique_devices = dcount(device_hash),
    page_loads = countif(ev == 'viewer_page_load'),
    heartbeats = countif(ev == 'viewer_heartbeat'),
    event_views = countif(ev == 'viewer_event_view'),
    median_lcp_ms = percentile(lcp_ms, 50),
    p90_lcp_ms = percentile(lcp_ms, 90)
  by meet_id, pi_local_date
| order by pi_local_date desc, unique_viewers desc
"""


def workspace_id_from_env() -> str:
    """Resolve the Log Analytics workspace ID from env. Notebook users
    typically set ``AZURE_LOG_ANALYTICS_WORKSPACE_ID`` in their shell."""
    wid = os.environ.get("AZURE_LOG_ANALYTICS_WORKSPACE_ID", "").strip()
    if not wid:
        raise RuntimeError(
            "Set AZURE_LOG_ANALYTICS_WORKSPACE_ID to the workspace GUID "
            "(Azure Portal > Log Analytics > Properties > Workspace ID)."
        )
    return wid


__all__ = [
    "BASE_PROJECT",
    "SESSION_STITCH",
    "run_kql",
    "per_meet_query",
    "cross_meet_query",
    "workspace_id_from_env",
]
