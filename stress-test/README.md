# CTS Scoreboard — Stress Test Pool

A pool of headless-Chromium browsers, hosted on Azure Container Apps Jobs,
that hammer a live meet page on the production site over the public internet
and exercise the Socket.IO `/scoreboard` namespace just like real users.

- **Compute**: Azure Container Apps **Job** (Manual trigger), one job per
  region. Each execution spawns N replicas; each replica drives M concurrent
  Playwright browsers in an open / hold / close / wait / reopen loop.
- **Isolation**: Lives in its own resource group(s), separate from
  `cts-scoreboard-preprod` / `cts-scoreboard-prod`. Same Azure subscription,
  same GitHub OIDC service principal.
- **Network path**: No VNet, no peering, no private endpoint. Traffic egresses
  to the main app's public ingress, the same path real browsers take.
- **Single or multi region**: Deploy to `westus` only, or `westus` + `eastus2`
  for geographically distributed load.

## Layout

```
stress-test/
├── README.md                  ← this file
├── app/
│   ├── Dockerfile             # Playwright Jammy base
│   ├── package.json
│   └── src/
│       ├── index.js           # entrypoint: parses env, fans out N VUs, heartbeat logs
│       └── browserCycle.js    # one VU's open/hold/close/wait loop (cold|hot|mixed)
└── infra/
    ├── main.bicep             # LAW + ACR + UAMI + AcrPull + CAE + Container Apps Job
    └── main.bicepparam        # default replica/cpu/memory values

.github/workflows/
├── stress-deploy.yml          # build + push image; deploy or what-if Bicep
├── stress-run.yml             # start one execution (per region) with chosen params
└── stress-stop.yml            # cancel all running executions
```

## One-time setup

The same GitHub OIDC SP that deploys the main app (the one used in
`azure-infra-deploy.yml`) drives this too. It needs role grants on the new
RG(s).

1. **Create resource group(s)**. The deploy workflow does this automatically,
   but doing it once up-front lets you grant roles in the same step.

   ```bash
   SUB_ID=$(az account show --query id -o tsv)
   az group create -n cts-scoreboard-stress-westus  -l westus
   # multi-region only:
   az group create -n cts-scoreboard-stress-eastus2 -l eastus2
   ```

2. **Grant the OIDC SP** Contributor + AcrPush + (constrained) RBAC Admin on
   each RG. Use the same `AZURE_CLIENT_ID` GUID stored in repo secrets and
   the same constrained RBAC pattern as `azure/docs/AZURE_SETUP.md` §4 — the
   constraint scopes role-creation rights to `AcrPull` only, which is what
   the Bicep needs for the user-assigned managed identity.

   ```bash
   GH_APP_ID="<value of AZURE_CLIENT_ID secret>"
   ACRPULL_DEF_ID="7f951dda-4ed3-4680-a7ca-43fe172d538d"
   COND="((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) \
   OR (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] \
   ForAnyOfAnyValues:GuidEquals {${ACRPULL_DEF_ID}}))"

   for RG in cts-scoreboard-stress-westus cts-scoreboard-stress-eastus2; do
     az role assignment create --assignee "$GH_APP_ID" --role Contributor \
       --scope "/subscriptions/${SUB_ID}/resourceGroups/${RG}"
     az role assignment create --assignee "$GH_APP_ID" --role AcrPush \
       --scope "/subscriptions/${SUB_ID}/resourceGroups/${RG}"
     az role assignment create --assignee "$GH_APP_ID" \
       --role "Role Based Access Control Administrator" \
       --scope "/subscriptions/${SUB_ID}/resourceGroups/${RG}" \
       --condition "$COND" --condition-version "2.0"
   done
   ```

3. **GitHub `stress` environment + OIDC federated credential**. The
   existing `preprod` / `production` federated subjects don't cover these
   workflows. The repo's `stress` environment is already created (with
   `h2orules` as required reviewer) and each stress workflow job pins
   `environment: stress`. You just need to add the matching federated
   credential to the Entra app registration:

   ```bash
   AZURE_CLIENT_ID="<value of AZURE_CLIENT_ID secret>"
   APP_OBJECT_ID=$(az ad app show --id "$AZURE_CLIENT_ID" --query id -o tsv)
   az rest --method POST \
     --uri "https://graph.microsoft.com/v1.0/applications/${APP_OBJECT_ID}/federatedIdentityCredentials" \
     --body '{
       "name": "github-stress",
       "issuer": "https://token.actions.githubusercontent.com",
       "subject": "repo:h2orules/CTS_Scoreboard:environment:stress",
       "audiences": ["api://AzureADTokenExchange"]
     }'
   ```

   No new secrets are required — the workflows reuse `AZURE_CLIENT_ID`,
   `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`.

   To manage the environment (e.g. change reviewers, switch to a team):
   `gh api repos/h2orules/CTS_Scoreboard/environments/stress` or in the
   GitHub UI under **Settings → Environments → stress**.

4. **First deploy**. Run **stress-deploy** with `mode=single`,
   `apply_or_whatif=apply`. This creates the RG (if missing), the ACR,
   builds + pushes the image with `az acr build`, and applies the Bicep.
   Subsequent runs are incremental.

## Running a stress test

Run the **stress-run** workflow from the GitHub Actions UI. Inputs:

| Input | Default | Notes |
|---|---|---|
| `target_url` | _required_ | Full URL of the meet page, e.g. `https://cts-sb-prod-app.westus.azurecontainerapps.io/web/home?meet=ABC` |
| `total_browsers` | 400 | 1-1000 |
| `browsers_per_replica` | 10 | 1-50; replicas = ceil(total / per-replica) |
| `min_hold_seconds` / `max_hold_seconds` | 30 / 120 | How long each browser stays connected before closing |
| `min_delay_seconds` / `max_delay_seconds` | 2 / 10 | Sleep between close and next open |
| `cache_mode` | `mixed` | `cold` (fresh browser each cycle), `hot` (persistent context), `mixed` (alternate) |
| `total_duration_seconds` | 600 | Wall-clock duration of the run |
| `mode` | `single` | `multi` splits load 50/50 across westus + eastus2 |

The workflow computes the per-region split, calls
`az containerapp job start --env-vars …` on each region's job, and prints
the execution names in the run summary.

### Picking sensible knobs

- **Burst / cold-cache test**: short `total_duration_seconds` (e.g. 60),
  `cache_mode=cold`, `min_hold=max_hold=30`. Every browser opens fresh, holds
  briefly, exits — measures cold-page TTI and ingress concurrency.
- **Long-running WebSocket test**: long `total_duration_seconds` (e.g. 3600),
  `cache_mode=hot`, `min_hold=300 max_hold=900`. Browsers stay connected for
  many minutes — measures Socket.IO fanout and replica memory growth on the
  main app.
- **Realistic mixed**: defaults — opens, varied hold, closes, reopens. Tests
  both the connection storm and the steady-state WebSocket pool.

## Stopping a test

Run **stress-stop** with the same `mode` you used to start. It lists every
execution in `Running` state and calls
`az containerapp job stop --execution-name <ex>` on each. Replicas receive
SIGTERM; the entrypoint exits within ~5s; ACA SIGKILLs after 30s.

## Reading results

Logs land in the per-region Log Analytics workspace
(`cts-sb-stress-<region>-la`) and stream live in the ACA portal under the job
execution. Useful KQL:

```kusto
ContainerAppConsoleLogs_CL
| where ContainerAppName_s startswith "cts-sb-stress-"
| where TimeGenerated > ago(1h)
| extend payload = parse_json(Log_s)
| where payload.msg in ("connected", "cycle-failed", "heartbeat", "replica-done")
| project TimeGenerated, ContainerAppName_s, ReplicaName_s,
          msg = tostring(payload.msg),
          ttwsMs = tolong(payload.ttws),
          err = tostring(payload.err),
          connects = tolong(payload.connects),
          failures = tolong(payload.failures)
| order by TimeGenerated asc
```

The main app's Application Insights also surfaces the load: look at request
counts, replica scale-out events, and the `pi_reconnects_total` /
`client_errors_total` custom metrics that already drive the existing alerts.

## Smoke test before going big

Always confirm the harness works end-to-end with a tiny run before launching
hundreds of browsers.

```bash
# Local smoke (no Azure required)
cd stress-test/app
docker build -t stress:dev .
docker run --rm \
  -e TARGET_URL="https://cts-sb-preprod-app.westus.azurecontainerapps.io/web/home" \
  -e BROWSERS_PER_REPLICA=2 \
  -e MIN_HOLD_SECONDS=5 -e MAX_HOLD_SECONDS=5 \
  -e MIN_DELAY_SECONDS=2 -e MAX_DELAY_SECONDS=2 \
  -e CACHE_MODE=cold -e TOTAL_DURATION_SECONDS=30 \
  stress:dev
```

Expect `connected` log lines (with `ttws` ms and a `wsUrl` containing
`/socket.io/?EIO=4`) followed by `replica-done` after ~30s.

Then ramp on Azure: `total_browsers=10` → `100` → `400`, watching the main
app's scale and alerts after each step.

## Cost estimate

Defaults: 400 browsers, 1 hour, single region (westus), Consumption profile.
List prices, no enterprise discounts.

| Item | Quantity | Subtotal |
|---|---|---|
| vCPU-seconds | 40 replicas × 2 vCPU × 3600 s = 288 000 | ~$7 |
| GiB-seconds | 40 × 4 GiB × 3600 = 576 000 | ~$2 |
| Log Analytics ingest | ~50 MB structured logs | <$1 |
| ACR Basic (per region) | flat | ~$0.17/day |
| Egress | ~5 Mbps × 400 × 3600 s ≈ 900 GB ≈ 800 GB billable | ~$70 |
| **Total ~ $80 / hour at 400 browsers** | | |

Egress dominates. 1000 browsers ≈ $200/hr. The 5 Mbps assumption is
conservative for a live race; an idle meet page is much quieter and the bill
drops linearly. Multi-region doubles the ACR baseline (~$0.34/day) and
egresses from two regions to the same prod app — cost scales with total
browsers, not the number of regions.

When idle (no executions running), the only ongoing cost is ACR Basic
(~$5/month per region) and Log Analytics retention (negligible). The job and
environment cost zero.

## Tradeoffs to know

- **Real browsers vs raw Socket.IO clients**: Real Chromium is ~10× more
  expensive per connection than a `socket.io-client` Node script, but it's
  the only way to test cold cache, JS execution, image/CSS fetches, and the
  actual handshake real users experience. Keep this as the default; consider
  adding a thin "ws-only" sibling later if you want a cheap way to push raw
  connection counts.
- **One ACR per region (Basic) vs geo-replicated single ACR (Premium)**:
  Geo-replicated ACR is Premium-only (~$1.66/day baseline vs ~$0.17 for
  Basic). Two Basic ACRs is cheaper and simpler for an occasional harness.
- **`replicaRetryLimit=0`**: A crashed replica is _not_ silently respawned —
  ACA marks the execution failed instead. That's intentional so retries don't
  skew load counts; check the Log Analytics `cycle-failed` events to
  understand why a replica died.
- **300-replica per-execution cap**: ACA Manual jobs default to a max
  parallelism of 300 per region. At `browsers_per_replica=10`, that caps a
  single region at 3 000 browsers per execution — well above the 1 000
  ceiling. If you ever need more density, raise `browsers_per_replica` or
  ask Azure support to lift the regional quota.
- **No anti-bot / WAF in front of the main app today**, so the load lands
  directly on Container Apps ingress. If a WAF is ever added, the egress IPs
  of the stress pool will rotate per execution; allowlist by job-execution
  outbound IPs (queryable via `az containerapp env show`) or whitelist the
  stress pool's NAT.
