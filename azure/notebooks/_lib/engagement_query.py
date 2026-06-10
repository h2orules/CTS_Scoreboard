"""Shared KQL + query helpers for the engagement notebooks.

All notebooks should import from here so the session-stitching logic is
defined exactly once: a viewing session is a run of ``viewer_heartbeat`` /
``viewer_event_view`` rows for a single ``(meet_id, pi_local_date,
viewer_id)`` triple where successive timestamps are <= 5 minutes apart.

Authentication uses :class:`DefaultAzureCredential` so the same notebook
works locally (``az login``) and in a managed identity context.

Heavy dependencies (pandas, azure-identity, azure-monitor-query,
ipywidgets) are imported lazily inside the functions that need them so the
pure query-building helpers stay importable in environments without the
``notebooks`` extra (e.g. unit tests).
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pandas as pd

# All engagement events go through customDimensions.viewer_event so the
# core scoreboard logs aren't mixed in. Each row is a single emit from
# engagement.js (page_load, heartbeat, event_view, message_board_view,
# hidden, visible, unload).
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
         mb_active = tobool(customDimensions.active),
         mb_page_index = toint(customDimensions.page_index),
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


def _client() -> Any:
    from azure.identity import DefaultAzureCredential
    from azure.monitor.query import LogsQueryClient

    return LogsQueryClient(DefaultAzureCredential())


def run_kql(
    workspace_id: str,
    query: str,
    *,
    days: int = 14,
    timespan: tuple[datetime, datetime] | None = None,
) -> pd.DataFrame:
    """Run a KQL query against Log Analytics and return a DataFrame.

    ``timespan`` (an absolute ``(start, end)`` window) takes precedence over
    the relative ``days`` lookback. Per-meet queries should always pass
    :func:`timespan_for_meet_day` here — a relative lookback silently
    returns 0 rows once the meet day falls outside the window.
    """
    import pandas as pd
    from azure.monitor.query import LogsQueryStatus

    client = _client()
    resp = client.query_workspace(
        workspace_id=workspace_id,
        query=query,
        timespan=timespan if timespan is not None else timedelta(days=days),
    )
    if resp.status != LogsQueryStatus.SUCCESS:  # type: ignore[attr-defined]
        raise RuntimeError(f"KQL query failed: {resp.partial_error or resp}")
    table = resp.tables[0]
    return pd.DataFrame(data=table.rows, columns=[c for c in table.columns])


def timespan_for_meet_day(pi_local_date: str) -> tuple[datetime, datetime]:
    """Absolute query window covering one Pi-local meet day.

    Derives the Log Analytics timespan from the meet date itself instead of
    a relative ``days=N`` lookback, so per-meet queries work no matter how
    long ago the meet ran. The window is padded by one day on each side to
    absorb skew between the Pi-local date and UTC ingestion timestamps.
    """
    day = datetime.strptime(pi_local_date, "%Y-%m-%d").replace(tzinfo=UTC)
    return day - timedelta(days=1), day + timedelta(days=2)


def per_meet_query(meet_id: str, pi_local_date: str) -> str:
    """Build the canonical per-meet engagement query for a single meet day.

    ``meet_id`` is compared case-insensitively (``=~``): meet IDs preserve
    the case the Pi registered, so an exact ``==`` against a hand-typed ID
    silently returns 0 rows on any case mismatch.
    """
    return f"""
traces
{BASE_PROJECT}
| where meet_id =~ '{meet_id}' and pi_local_date == '{pi_local_date}'
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
    message_board_views = countif(ev == 'viewer_message_board_view' and mb_active == true),
    median_lcp_ms = percentile(lcp_ms, 50),
    p90_lcp_ms = percentile(lcp_ms, 90)
  by meet_id, pi_local_date
| order by pi_local_date desc, unique_viewers desc
"""


def list_meets_query(*, days: int = 90) -> str:
    """Discovery query: every (meet_id, pi_local_date) seen in telemetry."""
    return f"""
traces
| where customDimensions has 'viewer_event'
| extend meet_id = tostring(customDimensions.meet_id),
         pi_local_date = tostring(customDimensions.pi_local_date)
| where isnotempty(meet_id) and isnotempty(pi_local_date)
| summarize events = count() by meet_id, pi_local_date
| order by pi_local_date desc, meet_id asc
"""


def list_meets(workspace_id: str, *, days: int = 90) -> pd.DataFrame:
    """Return the meets visible in telemetry over the last ``days`` days."""
    df = run_kql(workspace_id, list_meets_query(days=days), days=days)
    if len(df):
        df["label"] = (
            df["meet_id"] + " — " + df["pi_local_date"]
            + " (" + df["events"].astype(str) + " events)"
        )
    else:
        df["label"] = []
    return df


def meet_dropdown(workspace_id: str, *, days: int = 90) -> Any:
    """ipywidgets Dropdown of meets discovered from telemetry.

    ``widget.value`` is a ``(meet_id, pi_local_date)`` tuple, ready to feed
    into :func:`per_meet_query` / :func:`timespan_for_meet_day`.
    """
    import ipywidgets as widgets

    meets = list_meets(workspace_id, days=days)
    options = [
        (row.label, (row.meet_id, row.pi_local_date))
        for row in meets.itertuples()
    ]
    if not options:
        raise RuntimeError(
            f"No meets found in telemetry over the last {days} days. "
            "Increase `days` or check the workspace ID."
        )
    return widgets.Dropdown(
        options=options,
        description="Meet:",
        layout=widgets.Layout(width="32em"),
    )


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
    "cross_meet_query",
    "list_meets",
    "list_meets_query",
    "meet_dropdown",
    "per_meet_query",
    "run_kql",
    "timespan_for_meet_day",
    "workspace_id_from_env",
]
