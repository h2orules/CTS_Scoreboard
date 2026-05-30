# Azure Relay Dashboard Guide

Practical Kusto (KQL) queries for the metrics emitted by the relay
(`azure/app/telemetry.py` + `azure/app/scale_telemetry.py`), plus
step-by-step instructions for assembling them into an Azure Workbook /
Dashboard so you can watch a stress test live without round-tripping
through code changes.

> All queries target the **Application Insights** resource configured by
> `applicationinsights_connection_string`. Custom metrics land in the
> `customMetrics` table; the dimension you'll filter on most is
> `customDimensions["op"]` or `customDimensions["event"]`.

## How to build the dashboard

1. **Azure Portal → Application Insights → Logs.**
2. Paste a query from a section below and press *Run*.
3. Click **Pin to** (top right) → *To a dashboard* → choose a shared
   dashboard. Each query becomes one tile.
4. For richer layouts, use **Workbooks** instead: *Workbooks → New →
   Add → Query*, paste, set the visualization (Time chart, Stat, Bar,
   Grid), and save. A single workbook can hold all the panels below.
5. Set the workbook's *Time Range* to **Last 30 minutes** with a
   *5-second auto-refresh* during stress runs.

For each panel below the recommended visualization is in the heading.

---

## 1. Cache hit rate per op (time chart)

Tells you whether the per-replica `_TTLCache` is doing its job. Hit rate
should climb steadily once a meet has been live for a few minutes.

```kusto
customMetrics
| where name in ("cache_hits", "cache_misses")
| extend op = tostring(customDimensions["op"])
| summarize hits   = sumif(value, name == "cache_hits"),
            misses = sumif(value, name == "cache_misses")
        by bin(timestamp, 1m), op
| extend hit_rate = todouble(hits) / todouble(hits + misses)
| project timestamp, op, hit_rate
| render timechart
```

Healthy: `get_template_blob` and `get_current_template` > 0.95 within
a few minutes; `get_fragment` climbs to >0.7 during a heat.
A drop below 0.5 sustained = cache too small or TTL too short.

## 2. Redis op rate split by read vs write (time chart)

Confirms B1 coalescer is actually compressing writes, and surfaces any
read amplification from a new feature.

```kusto
let writes = dynamic(["put_state","put_fragment","put_template",
                      "put_context","open_meet","close_meet",
                      "heartbeat","mark_status"]);
customMetrics
| where name == "redis_op_seconds"
| extend op = tostring(customDimensions["op"])
| extend op_kind = iff(op in (writes), "write", "read")
| summarize ops = sum(valueCount) by bin(timestamp, 10s), op_kind
| render timechart
```

> `valueCount` is the per-bucket sample count from the OTel histogram;
> dividing by the bin size gives ops/sec. Watch the ratio during a
> stress test — coalescer-on should keep writes <30% of total even with
> many Pis active.

## 3. Redis server-side memory & evictions (multi-stat)

Sourced from the `INFO` poller in `scale_telemetry.py`. Lets you see
when Azure Cache for Redis is approaching its `maxmemory` cap, and
whether any keys are being evicted (which would silently corrupt state).

```kusto
customMetrics
| where name == "redis_memory_bytes"
| extend mem_kind = tostring(customDimensions["kind"])
| summarize bytes = avg(value) by bin(timestamp, 30s), mem_kind
| render timechart
```

Eviction & expiration counters (Stat tile, "delta since start"):

```kusto
customMetrics
| where name == "redis_keys_lifecycle_total"
| extend key_kind = tostring(customDimensions["kind"])
| summarize last = max(value), first = min(value) by key_kind
| extend delta = last - first
| project key_kind, delta
```

> `delta > 0` for `key_kind == "evicted"` is **always bad** — it means
> Redis dropped state for a live meet. Bump the SKU or shrink TTLs.

## 4. Redis clients & ops/sec (time chart)

```kusto
customMetrics
| where name in ("redis_clients", "redis_instantaneous_ops_per_sec")
| extend label = strcat(name, ":", tostring(customDimensions["state"]))
| summarize avg(value) by bin(timestamp, 30s), label
| render timechart
```

`connected_clients` should be ≤ `(workers per replica) × (replicas)
× max_connections + some Socket.IO pubsub`. If it spikes much higher,
something's leaking connections.

## 5. Per-replica fragment cache occupancy (time chart)

Shows whether the FIFO bound (`fragment_cache_max_entries`, default
1024) is being hit. Each replica reports independently — split by
`cloud_RoleInstance` to see imbalance.

```kusto
customMetrics
| where name == "cache_size_entries"
| extend cache = tostring(customDimensions["cache"])
| summarize entries = avg(value) by bin(timestamp, 30s),
                                     cache,
                                     cloud_RoleInstance
| render timechart
```

To check the configured cap (constant per deployment):

```kusto
customMetrics
| where name == "cache_max_entries"
| summarize max(value) by tostring(customDimensions["cache"])
```

If `entries` sits at `cache_max_entries` for long, raise the cap or
trim what's being cached.

## 6. Coalescer effectiveness ⭐ (time chart + stat)

> Use this during the reconnect-storm stress test. This is the most
> important panel for tuning `coalesce_window_seconds`.

**Compression ratio** (events per flushed batch, line chart):

```kusto
customMetrics
| where name in ("coalescer_events_in", "coalescer_batches_flushed")
| extend event = tostring(customDimensions["event"])
| summarize events_in = sumif(value, name == "coalescer_events_in"),
            batches  = sumif(value, name == "coalescer_batches_flushed")
        by bin(timestamp, 30s), event
| extend compression = todouble(events_in) / todouble(batches)
| project timestamp, event, compression
| render timechart
```

A `compression` of **1.0** means the coalescer didn't merge anything
(every Pi frame got its own emit) — equivalent to coalescer-off.
**5–20** is the sweet spot under reconnect-storm conditions: ~10×
reduction in emits, with sub-100ms latency. **>50** means the window
is too long for responsive UI; consider halving it.

**Batch size distribution** (stat or bar):

```kusto
customMetrics
| where name == "coalescer_batch_size"
| extend event = tostring(customDimensions["event"])
| summarize p50 = percentile(value, 50),
            p95 = percentile(value, 95),
            p99 = percentile(value, 99),
            mx  = max(value)
        by bin(timestamp, 1m), event
```

**Tuning rule of thumb during the reconnect storm:**

- p95 batch_size < 3 → window is shorter than the natural inter-frame
  gap; you're getting almost no compression. Increase
  `coalesce_window_seconds` (e.g. 0.1 → 0.2) and retest.
- p95 batch_size 5–15 → ideal; ratio gives ~5–15× emit reduction.
- p95 batch_size > 30 → too aggressive; UI feels laggy. Drop the
  window (e.g. 0.1 → 0.05).

## 7. HTTP route latency (auto-instrumented, Workbook chart)

FastAPI is auto-instrumented by `azure-monitor-opentelemetry`. These
land in the `requests` table, not `customMetrics`.

```kusto
requests
| where cloud_RoleName has "relay"
| summarize p50 = percentile(duration, 50),
            p95 = percentile(duration, 95),
            p99 = percentile(duration, 99),
            cnt = count()
        by bin(timestamp, 30s), name
| render timechart
```

Filter to viewer page loads only (where C1/C2 matters most):

```kusto
requests
| where name has "fragment" or name has "template" or name has "/view/"
| summarize p95 = percentile(duration, 95) by bin(timestamp, 30s), name
| render timechart
```

## 8. (Deferred) Socket.IO pub/sub depth

We don't have a dedicated probe yet. The proxy signal is the existing
`emit_fanout_seconds` histogram — if its p95 climbs while
`active_sockets` is flat, the AsyncRedisManager pub/sub subscriber is
backing up:

```kusto
customMetrics
| where name == "emit_fanout_seconds"
| summarize p50 = percentile(value, 50),
            p95 = percentile(value, 95)
        by bin(timestamp, 30s)
| render timechart
```

A dedicated `pubsub_channels` / `pubsub_patterns` gauge already lands
under `redis_pubsub` (item 4 source); a sustained increase there during
a reconnect storm corroborates backed-up fan-out.

---

## Pre-stress checklist

Before kicking off a reconnect-storm run, open the workbook with these
panels and confirm:

1. **Cache hit rate** ≥ 0.7 for `get_fragment` (warmed up).
2. **Coalescer compression** ≥ 3 on `update_scoreboard` (steady state
   with a single Pi connected and live race data flowing).
3. **Redis evictions delta** = 0.
4. **HTTP p95** on viewer routes < 300 ms.

If any of these are red before the stress test starts, the test results
will be hard to interpret — fix the baseline first.

## What to watch during the storm

- **Compression ratio** (panel 6) — should *increase* as the reconnect
  storm hits, because frames pile into each window faster.
- **active_sockets** (Socket.IO gauge in `customMetrics`) — should rise
  smoothly, no sawtooth.
- **redis_op_seconds.write rate** (panel 2 split) — should stay roughly
  flat even as Pi count grows (that's the whole point of coalescer +
  put_state dedupe).
- **emit_fanout_seconds p95** (panel 8) — must stay < 100 ms.
- **HTTP p95** — must stay < 1 s.

If `redis_op_seconds.write rate` climbs linearly with viewer count and
the coalescer ratio is *also* climbing, the bottleneck is downstream of
the coalescer (Socket.IO emit + AsyncRedisManager fan-out), not Redis
state writes — that's where to look next.
