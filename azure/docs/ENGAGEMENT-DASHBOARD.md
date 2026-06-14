# Engagement Dashboard Guide

KQL queries for the viewer-engagement telemetry emitted by `engagement.js`
and `app/handlers.py`, plus instructions for adding them to an Azure
Workbook alongside the existing infra/ops panels in [DASHBOARD.md](DASHBOARD.md).

> All queries target **Log Analytics** (the `traces` table) via the same
> Application Insights resource. The canonical Python versions of these
> queries live in `notebooks/_lib/engagement_query.py`; this document
> keeps them in sync so a change to one should be reflected in the other.

---

## Data model

Every engagement event is a `traces` row. The shared base projection is:

```kusto
traces
| where customDimensions has 'viewer_event'
| extend ev              = tostring(customDimensions.viewer_event),
         meet_id         = tostring(customDimensions.meet_id),
         pi_local_date   = tostring(customDimensions.pi_local_date),
         viewer_id       = tostring(customDimensions.viewer_id),
         device_hash     = tostring(customDimensions.device_hash),
         current_event   = tostring(customDimensions.current_event),
         current_heat    = tostring(customDimensions.current_heat),
         distance        = toint(customDimensions.distance),
         stroke_name     = tostring(customDimensions.stroke_name),
         age_group       = tostring(customDimensions.age_group),
         gender          = tostring(customDimensions.gender),
         relay           = tobool(customDimensions.relay),
         mb_active       = tobool(customDimensions.active),
         mb_page_index   = toint(customDimensions.page_index),
         tenure_ms       = tolong(customDimensions.tenure_ms),
         lcp_ms          = tolong(customDimensions.lcp_ms),
         effective_type  = tostring(customDimensions.effective_type)
```

`ev` values: `viewer_page_load`, `viewer_heartbeat`, `viewer_event_view`,
`viewer_message_board_view`, `viewer_hidden`, `viewer_visible`,
`viewer_unload`.

`viewer_message_board_view` fires when the message board overlay turns
on/off or rotates pages; `mb_active` / `mb_page_index` are only set on
those rows. It attributes viewing time on the board separately from the
last race shown.

**Session stitching** uses a 5-minute gap rule: successive rows for the same
`(viewer_id)` more than 300 seconds apart start a new session. This KQL
block is used by the per-meet panels:

```kusto
| sort by viewer_id asc, timestamp asc
| serialize
| extend prev_ts      = prev(timestamp),
         prev_viewer  = prev(viewer_id)
| extend new_session  = iff(viewer_id != prev_viewer
                            or datetime_diff('second', timestamp, prev_ts) > 300,
                            1, 0)
| extend session_idx  = row_cumsum(new_session)
```

**Privacy:** `viewer_id` is a per-tab UUID (sessionStorage only — never
persisted). `device_hash` is `sha256(salt|ip|ua)[:16]`; no PII is stored.

---

## How to add these panels

1. **Azure Portal → Application Insights → Workbooks → New** (or open the
   existing infra workbook from DASHBOARD.md).
2. **Add → Add query**, paste a KQL block below, set the visualization as
   noted in the heading, and click **Done editing**.
3. For the per-meet panels, first add a **Parameters** step with the
   meet-picker dropdowns described under
   [Meet-picker dropdown parameters](#meet-picker-dropdown-parameters), so
   the queries can reference `{meet_id}` and `{pi_local_date}` without
   hand-typing either value.
4. Set the Workbook **Time Range** to cover the meet you want to inspect
   (e.g. Last 24 hours). This is the most common cause of a per-meet query
   returning 0 rows: the `pi_local_date` filter only narrows *within* the
   time range — it does not extend it — so a meet older than the selected
   range matches nothing.

---

## Cross-meet panels

These use a rolling time window and need no parameters. Set the Workbook
time range to **Last 30 days** for a full-season view.

### 1. Meets summary (grid)

One row per meet day: unique viewers, page loads, event-view count, and
LCP percentiles. Use the **Grid** visualization; make columns sortable.

```kusto
traces
| where customDimensions has 'viewer_event'
| extend ev            = tostring(customDimensions.viewer_event),
         meet_id       = tostring(customDimensions.meet_id),
         pi_local_date = tostring(customDimensions.pi_local_date),
         viewer_id     = tostring(customDimensions.viewer_id),
         device_hash   = tostring(customDimensions.device_hash),
         mb_active     = tobool(customDimensions.active),
         lcp_ms        = tolong(customDimensions.lcp_ms)
| where isnotempty(meet_id) and isnotempty(pi_local_date)
| summarize
    unique_viewers  = dcount(viewer_id),
    unique_devices  = dcount(device_hash),
    page_loads      = countif(ev == 'viewer_page_load'),
    heartbeats      = countif(ev == 'viewer_heartbeat'),
    event_views     = countif(ev == 'viewer_event_view'),
    message_board_views = countif(ev == 'viewer_message_board_view' and mb_active == true),
    median_lcp_ms   = percentile(lcp_ms, 50),
    p90_lcp_ms      = percentile(lcp_ms, 90)
  by meet_id, pi_local_date
| order by pi_local_date desc, unique_viewers desc
```

### 2. Unique viewers per meet day (bar chart)

```kusto
traces
| where customDimensions has 'viewer_event'
| extend meet_id       = tostring(customDimensions.meet_id),
         pi_local_date = tostring(customDimensions.pi_local_date),
         viewer_id     = tostring(customDimensions.viewer_id)
| where isnotempty(meet_id) and isnotempty(pi_local_date)
| summarize unique_viewers = dcount(viewer_id) by pi_local_date
| order by pi_local_date asc
| render barchart
```

### 3. LCP p50 / p90 trend across meets (time chart)

Useful for catching regressions after a deploy. Points are per-meet-day
averages, not real-time.

```kusto
traces
| where customDimensions has 'viewer_event'
| extend ev            = tostring(customDimensions.viewer_event),
         pi_local_date = tostring(customDimensions.pi_local_date),
         lcp_ms        = tolong(customDimensions.lcp_ms)
| where ev in ('viewer_hidden', 'viewer_unload') and lcp_ms > 0
| summarize p50 = percentile(lcp_ms, 50),
            p90 = percentile(lcp_ms, 90)
        by pi_local_date
| order by pi_local_date asc
| render timechart
```

---

## Per-meet panels

These panels require `meet_id` and `pi_local_date` parameters; the Workbook
substitutes `{meet_id}` and `{pi_local_date}` in the queries below. Set
them up as query-backed dropdowns (next section) rather than Text
parameters — typing either value by hand is the easiest way to end up with
0 rows (typo, case mismatch, or a date with no telemetry).

### Meet-picker dropdown parameters

Two cascading dropdowns: pick the meet day first, then the meet on that
day. Because the second query references `{pi_local_date}`, the meet list
re-filters automatically when the date changes, and the panel queries below
work unchanged.

1. **Add → Add parameters** (or edit the existing Parameters step), then
   **Add Parameter** with:
   - **Parameter name:** `pi_local_date`
   - **Parameter type:** *Drop down*
   - **Required:** checked
   - **Get data from:** *Query*, with this KQL:

   ```kusto
   traces
   | where customDimensions has 'viewer_event'
   | extend pi_local_date = tostring(customDimensions.pi_local_date)
   | where isnotempty(pi_local_date)
   | summarize by pi_local_date
   | order by pi_local_date desc
   ```

   In the parameter's **Time Range** setting, pick a fixed long window
   (e.g. *Last 90 days*) instead of inheriting the workbook time range —
   otherwise old meets never show up in the dropdown, which is the same
   0-rows trap described above.

2. **Add Parameter** again for the meet:
   - **Parameter name:** `meet_id`
   - **Parameter type:** *Drop down*
   - **Required:** checked
   - **Get data from:** *Query*, with this KQL (note it references the
     first parameter, which is what makes the dropdowns cascade):

   ```kusto
   traces
   | where customDimensions has 'viewer_event'
   | extend meet_id       = tostring(customDimensions.meet_id),
            pi_local_date = tostring(customDimensions.pi_local_date)
   | where pi_local_date == '{pi_local_date}' and isnotempty(meet_id)
   | summarize events = count() by meet_id
   | order by events desc
   | project value = meet_id,
             label = strcat(meet_id, ' (', tostring(events), ' events)')
   ```

   Workbook dropdowns use the `value` column as the substituted parameter
   value and `label` as the display text, so `{meet_id}` resolves to the
   bare meet ID while the dropdown shows the event count alongside it. Set
   this parameter's **Time Range** to the same long window as the first.

3. Click **Done editing** on the Parameters step. The per-meet panels
   below will refresh whenever either dropdown changes. (The notebook
   equivalent is `meet_dropdown()` in
   `notebooks/_lib/engagement_query.py`, which combines both picks into a
   single list.)

### 4. Session summary (stat tiles)

Total sessions, median session length (seconds), and max concurrent
viewers in 1-minute buckets.

```kusto
traces
| where customDimensions has 'viewer_event'
| extend ev          = tostring(customDimensions.viewer_event),
         meet_id     = tostring(customDimensions.meet_id),
         pi_date     = tostring(customDimensions.pi_local_date),
         viewer_id   = tostring(customDimensions.viewer_id)
| where meet_id =~ '{meet_id}' and pi_date == '{pi_local_date}'
| sort by viewer_id asc, timestamp asc
| serialize
| extend prev_ts     = prev(timestamp), prev_viewer = prev(viewer_id)
| extend new_session = iff(viewer_id != prev_viewer
                           or datetime_diff('second', timestamp, prev_ts) > 300,
                           1, 0)
| extend session_idx = row_cumsum(new_session)
| summarize start_ts = min(timestamp), end_ts = max(timestamp)
        by viewer_id, session_idx
| extend duration_s = datetime_diff('second', end_ts, start_ts)
| summarize total_sessions  = count(),
            median_dur_s    = percentile(duration_s, 50),
            p90_dur_s       = percentile(duration_s, 90)
```

### 5. Concurrent viewers per minute (time chart)

Heartbeats are emitted every 30 s; count distinct `viewer_id` per
1-minute bucket to approximate a live concurrency curve.

```kusto
traces
| where customDimensions has 'viewer_event'
| extend ev        = tostring(customDimensions.viewer_event),
         meet_id   = tostring(customDimensions.meet_id),
         pi_date   = tostring(customDimensions.pi_local_date),
         viewer_id = tostring(customDimensions.viewer_id)
| where meet_id =~ '{meet_id}' and pi_date == '{pi_local_date}'
| where ev == 'viewer_heartbeat'
| summarize concurrent = dcount(viewer_id) by bin(timestamp, 1m)
| render timechart
```

### 6. Drop-off by event type (grid)

Which swim events kept the most viewers watching. Sort descending by
`unique_viewers` to find the most-watched event types.

```kusto
traces
| where customDimensions has 'viewer_event'
| extend ev          = tostring(customDimensions.viewer_event),
         meet_id     = tostring(customDimensions.meet_id),
         pi_date     = tostring(customDimensions.pi_local_date),
         viewer_id   = tostring(customDimensions.viewer_id),
         stroke_name = tostring(customDimensions.stroke_name),
         age_group   = tostring(customDimensions.age_group),
         gender      = tostring(customDimensions.gender)
| where meet_id =~ '{meet_id}' and pi_date == '{pi_local_date}'
| where ev == 'viewer_event_view'
| summarize unique_viewers = dcount(viewer_id),
            total_views    = count()
        by stroke_name, age_group, gender
| order by unique_viewers desc
```

### 7. Most-watched event attributes (grid)

Which distances, strokes, age groups, and genders drew the most unique viewers.
Use the **Grid** visualization; sort by `unique_viewers` descending.

```kusto
traces
| where customDimensions has 'viewer_event'
| extend ev          = tostring(customDimensions.viewer_event),
         meet_id     = tostring(customDimensions.meet_id),
         pi_date     = tostring(customDimensions.pi_local_date),
         viewer_id   = tostring(customDimensions.viewer_id),
         distance    = toint(customDimensions.distance),
         stroke_name = tostring(customDimensions.stroke_name),
         age_group   = tostring(customDimensions.age_group),
         gender      = tostring(customDimensions.gender),
         relay       = tobool(customDimensions.relay)
| where meet_id =~ '{meet_id}' and pi_date == '{pi_local_date}'
| where ev == 'viewer_event_view'
| summarize unique_viewers = dcount(viewer_id),
            total_views    = count()
        by distance, stroke_name, age_group, gender, relay
| extend event_label = strcat(tostring(distance), 'y ', gender, ' ', age_group,
                              ' ', stroke_name,
                              iff(relay, ' Relay', ''))
| project event_label, distance, stroke_name, age_group, gender, relay,
          unique_viewers, total_views
| order by unique_viewers desc
```

To rank by a single dimension across a whole season (e.g. "which distances
are most-watched overall"), drop the per-meet filters and collapse to one
attribute:

```kusto
traces
| where customDimensions has 'viewer_event'
| extend ev          = tostring(customDimensions.viewer_event),
         viewer_id   = tostring(customDimensions.viewer_id),
         distance    = toint(customDimensions.distance),
         stroke_name = tostring(customDimensions.stroke_name),
         age_group   = tostring(customDimensions.age_group),
         gender      = tostring(customDimensions.gender)
| where ev == 'viewer_event_view'
| summarize unique_viewers = dcount(viewer_id),
            total_views    = count()
        by distance, stroke_name, age_group, gender
| order by unique_viewers desc
| take 20
```

### 8. LCP and connection-type breakdown (grid)

Page-load quality per network type. `effective_type` comes from the
Network Information API in the browser (`4g`, `3g`, `2g`, `slow-2g`).

```kusto
traces
| where customDimensions has 'viewer_event'
| extend ev             = tostring(customDimensions.viewer_event),
         meet_id        = tostring(customDimensions.meet_id),
         pi_date        = tostring(customDimensions.pi_local_date),
         lcp_ms         = tolong(customDimensions.lcp_ms),
         effective_type = tostring(customDimensions.effective_type)
| where meet_id =~ '{meet_id}' and pi_date == '{pi_local_date}'
| where ev in ('viewer_hidden', 'viewer_unload') and lcp_ms > 0
| summarize page_loads   = count(),
            median_lcp   = percentile(lcp_ms, 50),
            p90_lcp      = percentile(lcp_ms, 90),
            p99_lcp      = percentile(lcp_ms, 99)
        by effective_type
| order by median_lcp asc
```

---

## Grafana alternative

If you have Grafana with the **Azure Monitor** data source:

1. Add a panel, choose the *Logs* query type under Azure Monitor, select
   your Log Analytics workspace, and paste any KQL block above.
2. For the cross-meet summary, the **Table** panel type maps directly to the
   Grid queries. For time-series panels, use **Time series**.
3. For per-meet drill-down, create Grafana variables (`meet_id`,
   `pi_local_date`) populated by a separate *List meets* query:

   ```kusto
   traces
   | where customDimensions has 'viewer_event'
   | extend meet_id       = tostring(customDimensions.meet_id),
            pi_local_date = tostring(customDimensions.pi_local_date)
   | where isnotempty(meet_id)
   | summarize by meet_id, pi_local_date
   | order by pi_local_date desc
   | project label = strcat(meet_id, ' (', pi_local_date, ')'),
             meet_id, pi_local_date
   ```

   Bind the `meet_id` and `pi_local_date` Grafana variables to the
   `meet_id` and `pi_local_date` columns from that query, then reference
   `$meet_id` / `$pi_local_date` in panel queries.

Grafana works best for the cross-meet trend panels. Azure Workbooks are
more convenient for the per-meet drill-down because parameter pickers
integrate natively without a separate variable-query step.

---

## Jupyter notebooks (offline / ad-hoc)

The same KQL is available as Python functions in
`notebooks/_lib/engagement_query.py`, called from:

- `notebooks/cross_meet.ipynb` — cross-meet rollup table + bar chart.
- `notebooks/per_meet.ipynb` — session stitching, drop-off table,
  concurrent-viewer curve, LCP summary.

See `notebooks/README.md` for setup (`uv sync --extra notebooks`,
`az login`, `AZURE_LOG_ANALYTICS_WORKSPACE_ID`). Use notebooks when you
need custom Python post-processing (pivot tables, matplotlib charts) that
goes beyond what Workbook/Grafana visualizations support.
