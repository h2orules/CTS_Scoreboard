# Azure Managed Redis

Issue #99 replaces Azure Cache for Redis Basic C0 with **Azure Managed Redis
Balanced B0, high availability disabled**, in the same region as the relay.
`infra/managed-redis.bicep` is the reusable cache module; `infra/main.bicep`
consumes its secure connection-string output. `redisSkuName` accepts only
`Balanced_B0` or `Balanced_B1`. The infra workflow exposes the same choices.
Anything larger needs explicit approval.

## Sizing and cost comparison

West US USD pay-as-you-go rates, retrieved 2026-09-09 from the
[Azure Retail Prices API](https://prices.azure.com/api/retail/prices).
Monthly estimates use 730 hours, exclude tax/network charges, and are before
the subscription's credit. Managed Redis rates below are for **one node**;
enabling HA adds another node.

| Capability | Legacy Basic C0 | Managed B0, non-HA | Managed B1, non-HA |
|---|---|---|---|
| Hourly price | $0.022 | **$0.016** | $0.032 |
| Monthly per environment | $16.06 | **$11.68** | $23.36 |
| Nominal memory | 250 MB | 0.5 GB | 1 GB |
| Memory available for data | Observed `maxmemory`: 285,000,000 bytes | Approximately 80% of nominal memory | Approximately 80% of nominal memory |
| Documented connection limit | 256 | 15,000 | 15,000 |
| HA / availability SLA | No | No | No |
| Client protocol | Non-cluster Redis | Non-cluster Redis (`NoCluster`) | Same |
| Main reason to choose | Retiring product | Lowest cost, already ample capacity | More memory if measured usage needs it |

**Choose B0 for both environments.** This reduces the two-cache baseline from
$32.12 to $23.36/month, saving $8.76/month (27.3%). B1 would cost **more**
than C0, not less, and is not justified by current usage.

Azure Monitor's available data in the seven-day window before migration showed
peaks below 0.6 MB, at most five connections and four operations/second. Both
databases were empty when inspected. These are idle/pre-release observations,
not a representative stress-test peak.

Microsoft's published Balanced 0.5 GB and 1 GB benchmarks both list approximately
120,000 GET requests/second. Those tests use **OSS clustering**, not our
`NoCluster` policy, and are not an SLA or a direct C0 comparison. We do not
claim that throughput for this app. Verify the app's actual operations and
compare latency on the two live services before removing C0; repeat stress
testing before increasing the expected viewer load. B1 primarily buys memory,
not a documented throughput improvement over B0.

Sources:
- [Managed Redis pricing](https://azure.microsoft.com/en-us/pricing/details/managed-redis/)
- [Legacy cache pricing](https://azure.microsoft.com/en-us/pricing/details/cache/)
- [Architecture, clustering and reserved memory](https://learn.microsoft.com/en-us/azure/redis/architecture)
- [Connection limits](https://learn.microsoft.com/en-us/azure/redis/overview)
- [Benchmark methodology and results](https://learn.microsoft.com/en-us/azure/redis/best-practices-performance)
- [Retirement FAQ](https://learn.microsoft.com/en-us/azure/azure-cache-for-redis/retirement-faq)

The Managed Redis pricing page has displayed a conflicting rounded B0 memory
size; use the overview's 0.5 GB tier and the deployed service's `INFO maxmemory`
for capacity decisions rather than assuming it has B1's memory.

## Compatibility choices

| Setting | Choice | Reason |
|---|---|---|
| Clustering | `NoCluster` | `redis.asyncio.Redis` and Socket.IO `AsyncRedisManager` use ordinary single-endpoint clients. Do not accept the service's `OSSCluster` default. |
| Database | `default`, logical database 0 | Preserves the existing key namespace and client URL. |
| Transport | TLS 1.2+, port 10000 | Managed Redis uses a different endpoint and port from the legacy cache's 6380. |
| Authentication | Access keys enabled explicitly | Preserves password authentication for both independent client pools. Passwords remain URL-encoded, never committed or logged. |
| Eviction | `VolatileLRU` | Matches the observed legacy policy. Only TTL-bearing keys are eligible for eviction. |
| Availability | HA disabled | Matches Basic C0's single-instance availability class; downtime/data loss during maintenance or failure remains possible. |
| Persistence/modules | Not enabled | No additional features or costs are needed for the existing command set. |

Keep `REDIS_URL` as the application interface. Local development still uses
`redis://localhost:6379/0`; Azure receives
`rediss://:<encoded-key>@<managed-host>:10000/0` through the
`managed-redis-conn` Container App secret. No cluster client, key renaming,
Socket.IO protocol change, dependency replacement, or Pi-side change is needed.
The bounded state-client pool remains at ten connections per worker; Socket.IO
uses a separate pool with no subscriber read timeout.

`/readyz` now sends a Redis PING and returns HTTP 503 if Redis is inaccessible.
`/healthz` remains a dependency-independent liveness endpoint. This prevents
an HTTP-only smoke test from declaring a broken cache migration successful.

## Safe rollout (preprod before prod)

**Do not start by applying the full main template against an unmigrated app.**
The old revisions reference `redis-conn`; removing that secret while they are
active can fail with `ContainerAppSecretInUse`.

1. Deploy only `infra/managed-redis.bicep` into the existing environment RG.
   Use name `cts-sb-<environment>-managed-redis` and SKU `Balanced_B0`. Leave
   the old `cts-sb-<environment>-redis` instance untouched.
2. Run the real-service contract below against old and new caches. Compare
   representative read/write latency from the same client. Verify memory,
   connections, INFO telemetry and Socket.IO publication.
3. Confirm no live Pi/meet is writing. Inspect the old database's key count.
   An empty cache requires no copy. **A nonempty cache is not automatically
   disposable:** it also contains two-week meet metadata and Pi-to-meet name
   ownership. Quiesce writers and migrate values with their remaining TTLs
   before cutover; never silently discard these records.
4. Add `managed-redis-conn` alongside `redis-conn`, then create a new Container
   App revision changing only `REDIS_URL` to
   `secretref:managed-redis-conn`. Keep the old revision/secret/cache for
   rollback. Pin traffic explicitly to the new revision once ready.
5. Run the contract and dependency-aware readiness probe against the new
   service, and check the custom-domain HTTP/Socket.IO surface. Do not split
   traffic between old-cache and new-cache revisions: their state and pub/sub
   are isolated. A short reconnect window is preferable to split-brain.
6. Deactivate old-cache revisions. Apply the full main template to reconcile
   configuration and remove the unused old secret. Confirm the active revision
   and any revisions kept for rollback all use Managed Redis.
7. Only after the new deployment is healthy, delete the specific legacy
   `Microsoft.Cache/Redis` resource. Never delete the shared resource group.
   Repeat for prod after preprod succeeds.

Before step 6, rollback means route all traffic back to the old revision
and deactivate the new one; its original secret/cache remain intact. Once
new writes have started, reconcile state before rollback instead of sending
clients to a stale cache. After removing the old cache, rollback must use
Managed Redis, not a revision containing the old connection reference.

Secret updates alone do not restart running Container App replicas. The
changed **secretRef in the revision template** is intentional; it creates a
fresh revision that actually reads the new credential/endpoint.

## Validation

Run from `azure/`:

```bash
uv run ruff check app tests
uv run mypy app
uv run pytest tests/unit tests/integration -q
az bicep build --file infra/main.bicep --stdout > /dev/null
```

The existing strict mypy baseline contains unrelated errors (also present
before #99); CI currently treats mypy as advisory. Do not hide new errors or
weaken its configuration as part of this migration.

For the opt-in real-service test, inject `REDIS_TEST_URL` securely into the
process environment (do not put credentials in shell history or logs):

```bash
uv run pytest tests/integration/test_redis_service.py -q
```

It uses unique test keys and a private Socket.IO channel, removes only its
own keys, and never runs FLUSHDB. It exercises state merges/pipelines, expiry,
templates, fragments, context, meet ownership, SCAN, multi-key deletion, INFO,
and Socket.IO publication observed by a separate Redis client. Ordinary CI
skips this test when no service URL is supplied.
