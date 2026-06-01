# Engagement notebooks

Jupyter notebooks for analysing viewer-engagement telemetry emitted by the
Azure-served scoreboard (`/m/{meet_id}` pages).

## Setup

```bash
cd azure
uv sync --extra notebooks
az login                # so DefaultAzureCredential can hit Log Analytics
export AZURE_LOG_ANALYTICS_WORKSPACE_ID=<workspace-guid>
uv run jupyter lab notebooks/
```

The workspace GUID is on the Log Analytics resource > Properties >
"Workspace ID" (not the resource ID).

## Notebooks

- `per_meet.ipynb` — drill into a single `(meet_id, pi_local_date)`: viewing
  sessions stitched with the shared 5-minute gap rule, drop-off curve by
  event type, concurrent-viewer peak, LCP / connection-type histograms.
- `cross_meet.ipynb` — rollup across all meets in the last N days: unique
  viewers per meet, page-load count, LCP p50/p90, sortable table.

Both notebooks import `_lib/engagement_query.py` so the session-stitching
KQL is defined exactly once.

## Privacy / identity

Each row carries:

- `viewer_id` — per-tab UUID (sessionStorage), uncorrelated across tabs.
- `device_hash` — server-computed `sha256(salt|ip|ua)[:16]`. Rotate
  `AZURE_TELEMETRY_SALT` to invalidate every existing hash.

No cookies, no PII, no precise geo. IP is consumed only for the hash and
then discarded.
