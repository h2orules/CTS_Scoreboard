# Engagement notebooks

Jupyter notebooks for analysing viewer-engagement telemetry emitted by the
Azure-served scoreboard (`/m/{meet_id}` pages).

Three ways to run them — pick one:

## Run in GitHub Codespaces (no local setup)

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/h2orules/CTS_Scoreboard?quickstart=1)

The repo devcontainer (`.devcontainer/devcontainer.json`) preinstalls the
Azure CLI, the notebook dependencies, and the Jupyter extension.

1. Create a codespace (badge above, or **Code → Codespaces** on the repo).
   On creation, Codespaces offers to set the
   `AZURE_LOG_ANALYTICS_WORKSPACE_ID` secret — paste the workspace GUID
   there once and every future codespace gets it as an env var. (Or
   `export` it in the terminal instead.)
2. In the codespace terminal: `az login --use-device-code`.
3. Open `azure/notebooks/per_meet.ipynb` and **Run All**. The meet
   dropdown and charts render natively in the notebook UI.

## Run in VS Code (local)

Open the repo (or `cts-scoreboard.code-workspace`) in VS Code with the
Python + Jupyter extensions (both are in the workspace recommendations):

1. **Terminal → Run Task → "Notebooks: install deps"** — creates
   `azure/.venv` with the notebook extras.
2. `az login` and set `AZURE_LOG_ANALYTICS_WORKSPACE_ID` (see below).
3. Open a notebook and select the `azure/.venv` interpreter as its kernel,
   or use **Run Task → "Notebooks: launch Jupyter Lab"** to work in the
   browser instead.

Alternatively, with the **Dev Containers** extension you can "Reopen in
Container" to reuse the exact Codespaces environment locally.

## Run from the shell

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

- `per_meet.ipynb` — drill into a single `(meet_id, pi_local_date)`,
  selected from a dropdown of meets discovered in telemetry: viewing
  sessions stitched with the shared 5-minute gap rule, drop-off curve by
  event type, concurrent-viewer peak, message-board engagement, LCP /
  connection-type histograms. The query window is derived from the meet's
  own date (`timespan_for_meet_day`), so meets older than any relative
  lookback still return data, and the meet ID is matched
  case-insensitively.
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
