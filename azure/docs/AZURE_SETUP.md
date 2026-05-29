# Azure Setup — CTS Scoreboard Relay

This document walks through provisioning all Azure resources for the CTS
Scoreboard relay front-end **from scratch** using the `az` CLI. Run every
command on your workstation; nothing executes on the Pi.

> **Security note** — Alert recipient contact info (your email and phone) and
> all secrets are passed at deploy time **only**. They are never committed to
> source. Anywhere this doc shows `<your-...>`, that value lives in your shell
> session or in GitHub repo secrets, not in a file in the repo.

---

## 0. Prerequisites

```bash
# Required tooling
az --version            # >= 2.60
az bicep version        # az will install on first use
gh --version            # GitHub CLI, for setting repo secrets
docker --version        # only needed for local image testing

# Sign in
az login
az account set --subscription "<your-subscription-id>"
```

You should be repo-owner of `STU940652/CTS_Scoreboard` on GitHub (or use a
fork) so you can configure secrets and Environments.

---
 
## 1. Pick names and a region

These names are referenced throughout. Override to taste; everything else
chains off them.

```bash
export LOCATION="westus2"
export RG_PREPROD="cts-scoreboard-preprod"
export RG_PROD="cts-scoreboard-prod"

# Recipients — DO NOT COMMIT. Set in your shell only.
export ALERT_EMAIL="<your.email@example.com>"
export ALERT_SMS_COUNTRY_CODE="1"
export ALERT_SMS_PHONE="<10-digit-number-no-formatting>"
```

---

## 2. Create resource groups

```bash
az group create -n "$RG_PREPROD" -l "$LOCATION"
az group create -n "$RG_PROD"    -l "$LOCATION"
```

---

## 3. Entra ID — relay app registration (Pi → Azure auth)

The Pi authenticates to Azure via the OAuth2 device-code flow. You need an
Entra **App Registration** that the Pi obtains a token for, and the relay app
validates that token on its `/pi` Socket.IO namespace.

```bash
# Create the app registration. --sign-in-audience AzureADMyOrg keeps it
# scoped to your tenant.
RELAY_APP=$(az ad app create \
  --display-name "CTS Scoreboard Relay" \
  --sign-in-audience AzureADMyOrg \
  --is-fallback-public-client true)
RELAY_APP_ID=$(echo "$RELAY_APP" | jq -r .appId)
echo "Relay app (audience) id: $RELAY_APP_ID"

# Allow the device-code flow.
az ad app update --id "$RELAY_APP_ID" --set publicClient='{"redirectUris":["https://login.microsoftonline.com/common/oauth2/nativeclient"]}'

# Expose an API scope so the Pi requests a delegated token.
SCOPE_GUID=$(uuidgen)
az ad app update --id "$RELAY_APP_ID" --identifier-uris "api://$RELAY_APP_ID"
az rest --method PATCH \
  --uri "https://graph.microsoft.com/v1.0/applications(appId='$RELAY_APP_ID')" \
  --headers "Content-Type=application/json" \
  --body "{
    \"api\": {
      \"oauth2PermissionScopes\": [{
        \"id\": \"$SCOPE_GUID\",
        \"adminConsentDisplayName\": \"Connect Pi to relay\",
        \"adminConsentDescription\": \"Allow the on-site Pi to relay live scoreboard data.\",
        \"userConsentDisplayName\": \"Connect this Pi to relay\",
        \"userConsentDescription\": \"Allow this Pi to relay live scoreboard data.\",
        \"value\": \"Pi.Connect\",
        \"type\": \"User\",
        \"isEnabled\": true
      }]
    }
  }"

TENANT_ID=$(az account show --query tenantId -o tsv)
echo "TENANT_ID=$TENANT_ID"
echo "RELAY_APP_ID=$RELAY_APP_ID"
```

> **Re-running on an existing app?** If the `Pi.Connect` scope already
> exists, the PATCH above fails with
> `CannotDeleteOrUpdateEnabledEntitlement` — Graph won't replace an
> *enabled* scope in one shot. Skip the PATCH (the scope is already
> there) and continue with the grant below. To genuinely modify the
> existing scope, PATCH it once with `"isEnabled": false`, then PATCH
> again with the new shape and `"isEnabled": true`.

### 3a. Grant the relay app the `Pi.Connect` scope on itself

The Pi requests a token for the relay app using its own client id (i.e. it
asks for a token whose resource is itself). MSAL/AAD requires that
combination of audience+scope to be listed under the app's API permissions
*and* admin-consented; otherwise sign-in fails with `AADSTS650057 Invalid
resource ... List of valid resources from app registration: .` (note the
empty list).

```bash
# Look up the scope id (works whether the scope was just created or already existed).
SCOPE_ID=$(az ad app show --id "$RELAY_APP_ID" \
  --query "api.oauth2PermissionScopes[?value=='Pi.Connect'].id | [0]" -o tsv)
echo "SCOPE_ID=$SCOPE_ID"

# Make sure a service principal exists for the app in this tenant.
az ad sp create --id "$RELAY_APP_ID" 2>/dev/null || true

# Add Pi.Connect as a required delegated permission ON ITSELF.
az ad app permission add --id "$RELAY_APP_ID" \
  --api "$RELAY_APP_ID" \
  --api-permissions "${SCOPE_ID}=Scope"

# Admin-consent so `.default` has something to issue.
az ad app permission grant --id "$RELAY_APP_ID" \
  --api "$RELAY_APP_ID" \
  --scope "Pi.Connect"
```

Record `TENANT_ID` and `RELAY_APP_ID` — you'll set them as Bicep parameters
(`entraTenantId`, `entraAudience`) in steps 7 and 8. `entraAudience` should
be passed as `api://$RELAY_APP_ID` (the Application ID URI form), since
that's the `aud` claim AAD writes when the Pi requests a token for the
named `Pi.Connect` scope. The relay's validator also accepts the bare GUID
form, so existing deployments will continue to work. The Pi-side settings
UI also asks for these once at sign-in time.

---

## 4. Entra ID — GitHub Actions OIDC federation

GitHub Actions logs in to Azure using a federated credential — no client
secrets to rotate.

```bash
# Service principal app registration for GH Actions.
GH_APP=$(az ad app create --display-name "CTS Scoreboard - AquaGnome Apps")
GH_APP_ID=$(echo "$GH_APP" | jq -r .appId)
GH_SP=$(az ad sp create --id "$GH_APP_ID")
GH_SP_OBJECT_ID=$(echo "$GH_SP" | jq -r .id)

# Grant Contributor on each resource group.
SUB_ID=$(az account show --query id -o tsv)
az role assignment create --assignee "$GH_APP_ID" --role Contributor \
  --scope "/subscriptions/$SUB_ID/resourceGroups/$RG_PREPROD"
az role assignment create --assignee "$GH_APP_ID" --role Contributor \
  --scope "/subscriptions/$SUB_ID/resourceGroups/$RG_PROD"
# AcrPush so workflows can push images.
az role assignment create --assignee "$GH_APP_ID" --role AcrPush \
  --scope "/subscriptions/$SUB_ID/resourceGroups/$RG_PREPROD"
az role assignment create --assignee "$GH_APP_ID" --role AcrPush \
  --scope "/subscriptions/$SUB_ID/resourceGroups/$RG_PROD"

# Role Based Access Control Administrator (constrained) so the
# azure-infra-deploy workflow can (re)create the AcrPull role assignment
# the Bicep template declares for the user-assigned managed identity.
# Without this, `az deployment group create` fails with:
#   Authorization failed ... action 'Microsoft.Authorization/roleAssignments/write'
# The condition restricts the SP to assigning ONLY the AcrPull role
# (7f951dda-4ed3-4680-a7ca-43fe172d538d), so a compromised CI token
# can't grant itself Owner.
ACR_PULL_ROLE_ID="7f951dda-4ed3-4680-a7ca-43fe172d538d"
RBAC_ADMIN_ROLE_ID="f58310d9-a9f6-439a-9e8d-f62e7b41a168"
RBAC_CONDITION="((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {$ACR_PULL_ROLE_ID})) AND ((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})) OR (@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {$ACR_PULL_ROLE_ID}))"
for RG in "$RG_PREPROD" "$RG_PROD"; do
  az role assignment create \
    --assignee "$GH_APP_ID" \
    --role "$RBAC_ADMIN_ROLE_ID" \
    --scope "/subscriptions/$SUB_ID/resourceGroups/$RG" \
    --description "Allow GH Actions to create AcrPull role assignments only" \
    --condition "$RBAC_CONDITION" \
    --condition-version "2.0"
done

# Federated credentials — one per GitHub Environment.
GH_REPO="h2orules/CTS_Scoreboard"

for ENV_NAME in preprod production; do
  az ad app federated-credential create --id "$GH_APP_ID" --parameters "{
    \"name\": \"github-$ENV_NAME\",
    \"issuer\": \"https://token.actions.githubusercontent.com\",
    \"subject\": \"repo:$GH_REPO:environment:$ENV_NAME\",
    \"audiences\": [\"api://AzureADTokenExchange\"]
  }"
done

echo "AZURE_CLIENT_ID=$GH_APP_ID"
echo "AZURE_TENANT_ID=$TENANT_ID"
echo "AZURE_SUBSCRIPTION_ID=$SUB_ID"
```

---

## 5. GitHub repo configuration

```bash
gh repo set-default "$GH_REPO"

# Repo-level secrets used by every workflow.
gh secret set AZURE_CLIENT_ID         --body "$GH_APP_ID"
gh secret set AZURE_TENANT_ID         --body "$TENANT_ID"
gh secret set AZURE_SUBSCRIPTION_ID   --body "$SUB_ID"
# Used by azure-infra-deploy to pass entraAudience=api://$RELAY_APP_ID to
# the Bicep template. (entraTenantId reuses AZURE_TENANT_ID.)
gh secret set RELAY_APP_ID            --body "$RELAY_APP_ID"

# Alert recipients — never commit, only stored as secrets.
gh secret set ALERT_EMAIL             --body "$ALERT_EMAIL"
gh secret set ALERT_SMS_COUNTRY_CODE  --body "$ALERT_SMS_COUNTRY_CODE"
gh secret set ALERT_SMS_PHONE         --body "$ALERT_SMS_PHONE"

# Resource group names per environment.
gh secret set RG_PREPROD --body "$RG_PREPROD"
gh secret set RG_PROD    --body "$RG_PROD"

# Create the GitHub Environments. The 'production' environment must require a
# reviewer — set this once via the web UI (Settings → Environments → production
# → Required reviewers → add yourself). The CLI does not yet expose this.
gh api -X PUT "repos/$GH_REPO/environments/preprod" >/dev/null
gh api -X PUT "repos/$GH_REPO/environments/production" >/dev/null
```

After running these commands, open
`https://github.com/$GH_REPO/settings/environments/production` and tick
**Required reviewers** → add yourself. This is the manual approval gate
between pre-prod and prod.

---

## 6. First-time bootstrap of the ACR (preprod and prod)

The Bicep template below creates the ACR, but the workflow needs to push an
initial image **before** the Container App can be deployed pointing at it.
Bootstrap with a tiny placeholder.

```bash
# Pre-prod bootstrap.
ACR_PREPROD="ctssbpreprodacr"
az acr create -g "$RG_PREPROD" -n "$ACR_PREPROD" --sku Basic
az acr import --name "$ACR_PREPROD" --source mcr.microsoft.com/azuredocs/aci-helloworld:latest \
  --image cts-relay:bootstrap

# Prod bootstrap.
ACR_PROD="ctssbprodacr"
az acr create -g "$RG_PROD" -n "$ACR_PROD" --sku Basic
az acr import --name "$ACR_PROD" --source mcr.microsoft.com/azuredocs/aci-helloworld:latest \
  --image cts-relay:bootstrap
```

After the first real workflow deploy, this bootstrap image is replaced.

---

## 7. Deploy pre-prod via Bicep (one-time, then via workflow)

The first deploy creates every other resource (Redis, Storage,
Log Analytics, App Insights, Container Apps Environment, Container App,
Action Group, Alert Rules). Subsequent updates to the relay app go through
the GitHub Actions workflow.

```bash
az deployment group create \
  -g "$RG_PREPROD" \
  --template-file azure/infra/main.bicep \
  --parameters \
      environmentName=preprod \
      containerImage="$ACR_PREPROD.azurecr.io/cts-relay:bootstrap" \
      targetPort=80 \
      entraTenantId="$TENANT_ID" \
      entraAudience="api://$RELAY_APP_ID" \
      alertEmail="$ALERT_EMAIL" \
      alertSmsCountryCode="$ALERT_SMS_COUNTRY_CODE" \
      alertSmsPhone="$ALERT_SMS_PHONE"
```

> The `targetPort=80` override is only needed for the bootstrap image
> (`aci-helloworld` listens on port 80). After the first real workflow
> deploy in section 9, re-run this command without `targetPort=...` so the
> ingress flips back to the relay's port 8000.

Confirm `outputs.containerAppFqdn` resolves over HTTPS:

```bash
PREPROD_FQDN=$(az deployment group show -g "$RG_PREPROD" -n main \
  --query properties.outputs.containerAppFqdn.value -o tsv)
curl -fsS "https://$PREPROD_FQDN/healthz"   # bootstrap image; will 404 until first real deploy
```

---

## 8. Deploy prod (one-time bootstrap)

```bash
az deployment group create \
  -g "$RG_PROD" \
  --template-file azure/infra/main.bicep \
  --parameters \
      environmentName=prod \
      containerImage="$ACR_PROD.azurecr.io/cts-relay:bootstrap" \
      targetPort=80 \
      entraTenantId="$TENANT_ID" \
      entraAudience="api://$RELAY_APP_ID" \
      alertEmail="$ALERT_EMAIL" \
      alertSmsCountryCode="$ALERT_SMS_COUNTRY_CODE" \
      alertSmsPhone="$ALERT_SMS_PHONE"
```

---

## 9. First real deploy from GitHub Actions

1. Push to a branch and open a PR — `azure-ci.yml` runs lint + unit tests +
   container build (no deploy).
2. Merge to `master`.
3. Run **`azure-deploy-preprod`** workflow manually (Actions tab → "Run
   workflow"). It logs in via OIDC, builds and pushes the image to
   `$ACR_PREPROD`, then updates the pre-prod Container App revision to that
   digest. Workflow output prints the pre-prod URL.
4. Smoke test the pre-prod URL.
5. Run **`azure-promote-prod`** workflow manually with the digest from the
   pre-prod run. The `production` environment will block until you approve.

### Deploying infrastructure changes (Bicep)

The `azure-deploy-preprod` / `azure-promote-prod` workflows only update the
container image. To redeploy `azure/infra/main.bicep` itself (replica
counts, env vars, alert rules, ingress, etc.), use the
**`azure-infra-deploy`** workflow:

1. Actions tab → "azure-infra-deploy" → Run workflow.
2. Pick `environment: preprod` (or `prod`) and `mode: what-if` first.
3. Review the diff in the workflow log.
4. Re-run with `mode: apply` to deploy.

The workflow reads the currently-running container image off the live
Container App and pins the Bicep deploy to it, so infra changes never
inadvertently roll the app back to a stale image. Targeting `prod` goes
through the same required-reviewer gate as `azure-promote-prod`.

---

## 10. Updating alert recipients (you, post-deploy, no workflow needed)

```bash
ENV=preprod   # or prod
RG="cts-scoreboard-$ENV"

az monitor action-group update \
  -g "$RG" \
  -n "cts-sb-$ENV-ag" \
  --add-action email primary-email "<new.email@example.com>" \
  --add-action sms   primary-sms   1 5551234567
```

(`--add-action` removes the old receiver of the same name and adds the new
one; nothing in source changes.)

---

## 11. Rollback

```bash
ENV=prod
RG="cts-scoreboard-$ENV"
APP="cts-sb-$ENV-app"

# List the last few revisions.
az containerapp revision list -g "$RG" -n "$APP" \
  --query "[].{name:name, active:properties.active, created:properties.createdTime}" -o table

# Activate a previous revision (also deactivates the current one).
az containerapp revision activate -g "$RG" -n "$APP" --revision "<previous-revision-name>"
```

---

## 12. Tear down (preprod, on demand)

The `azure-deploy-preprod` workflow defaults `minReplicas=0` so a quiescent
pre-prod costs only the storage + Redis Basic baseline.
Full teardown:

```bash
az group delete -n "$RG_PREPROD" --yes --no-wait
```

Prod should never be torn down through this command.

---

## Cost sanity check

Idle pre-prod (everything scaled to zero except the always-on resources):

| Resource | Tier | ~Monthly |
|---|---|---|
| Container Apps | min=0 | $0 when idle |
| Redis | Basic C0 | ~$16 |
| Storage | Standard_LRS, low usage | <$1 |
| App Insights | first 5 GB free | $0 |
| Log Analytics | included with App Insights | $0 |
| **Total** | | **~$17/mo** when idle |

Prod with light real usage (1 meet/day, 50 viewers) is roughly the same plus
Container Apps active time (typically a few dollars/month at this scale).

---

## Architecture decision: Socket.IO fanout

Browser viewers connect via Socket.IO directly to the Container App. Cross-
worker / cross-replica fanout is provided by `socketio.AsyncRedisManager`
on the same Redis instance used for meet state. Combined with WebSocket-
only transport (no sticky sessions required), this scales horizontally
across the Container App's max replicas.

**Azure Web PubSub for Socket.IO was considered and rejected.** WPS bills
per outgoing message, and a fanout to N viewers counts as N messages.
For our workload (10–30 events/sec/meet × hundreds-to-thousands of
viewers × multiple concurrent meets), the per-message bill scales into
the hundreds of thousands of dollars per month at peak — 100×–1000×
more than the current Container Apps + Redis design — while delivering no
latency or reliability win for our shape. Revisit only if a single meet
sustains >5K viewers or the relay sustains >1M outbound msg/s for long
periods.

---

## Performance metrics and dashboards

The relay emits OpenTelemetry counters, histograms, and up-down counters
that flow to Application Insights as `customMetrics`. Counters/up-down
counters show up under `name` directly; histograms show up with
`valueSum`, `valueCount`, `valueMin`, and `valueMax`. The
`customDimensions` JSON column carries the per-event tags.

Metric inventory (in `app/telemetry.py`):

| Metric | Type | Tags | What it measures |
|---|---|---|---|
| `meets_opened` / `meets_closed` / `meets_degraded` | counter | `meet_id` | meet lifecycle |
| `browsers_connected` / `browsers_disconnected` | counter | `meet_id` | viewer arrival rate |
| `relay_events_processed` | counter | `event` | Pi-event processing rate |
| `event_handler_seconds` | histogram | `event`, `namespace` | end-to-end handler latency |
| `redis_op_seconds` | histogram | `op` | per-logical-Redis-op latency |
| `emit_fanout_seconds` | histogram | `event`, `namespace` | `await sio.emit(...)` duration (Redis pub/sub fan-out) |
| `active_sockets` | up-down counter | `namespace` | current open WebSocket count |
| `pi_connections` | up-down counter | — | current Pi sources connected |

### Verify metrics are arriving

In Application Insights → **Logs**:

```kql
customMetrics
| where timestamp > ago(15m)
| where name in ("event_handler_seconds", "redis_op_seconds",
                 "emit_fanout_seconds", "active_sockets",
                 "pi_connections")
| summarize count() by name
```

If `count()` is non-zero for each metric, the pipeline is healthy.

### Useful KQL queries

Per-event handler p50/p95/p99 (last hour):

```kql
customMetrics
| where timestamp > ago(1h)
| where name == "event_handler_seconds"
| extend event = tostring(customDimensions.event)
| summarize p50=percentile(value, 50),
            p95=percentile(value, 95),
            p99=percentile(value, 99)
          by event, bin(timestamp, 1m)
| render timechart
```

Redis op p95 latency by op:

```kql
customMetrics
| where timestamp > ago(1h)
| where name == "redis_op_seconds"
| extend op = tostring(customDimensions.op)
| summarize p95=percentile(value, 95) by op, bin(timestamp, 1m)
| render timechart
```

Active sockets per namespace (rolling sum of up-down deltas):

```kql
customMetrics
| where timestamp > ago(1h)
| where name == "active_sockets"
| extend ns = tostring(customDimensions.namespace)
| summarize sockets = sum(valueSum) by ns, bin(timestamp, 1m)
| render timechart
```

Fan-out latency vs. concurrent viewer count (correlation):

```kql
let fanout = customMetrics
  | where name == "emit_fanout_seconds" and timestamp > ago(1h)
  | summarize p95=percentile(value, 95) by bin(timestamp, 1m);
let sockets = customMetrics
  | where name == "active_sockets"
        and tostring(customDimensions.namespace) == "/scoreboard"
        and timestamp > ago(1h)
  | summarize sockets = sum(valueSum) by bin(timestamp, 1m);
fanout
| join sockets on timestamp
| project timestamp, p95, sockets
| render timechart
```

### Suggested alerts

In Application Insights → **Alerts** → **Create alert rule** (signal type
"Custom log search"):

1. **High event handler p95** — `event_handler_seconds` p95 > 0.150 s
   for 5 min; severity 3.
2. **Redis op tail latency** — `redis_op_seconds` p99 > 0.050 s for 5 min;
   severity 2 (likely Redis tier saturation).
3. **Fan-out backpressure** — `emit_fanout_seconds` p95 > 0.250 s for
   5 min; severity 2.
4. **Pi gone with viewers present** — `pi_connections` sum < 1 AND
   `active_sockets` (`/scoreboard`) > 50 for 2 min; severity 1.

### Workbook (recommended)

Application Insights → **Workbooks** → **New** → paste each KQL query
above into a separate timechart tile. Save as "Relay Performance" so
on-call has one dashboard.

---

## Scaling: KEDA `azure-monitor` rule on `active_sockets`

The Container App ships with two scale rules; KEDA scales to the
**maximum** desired-replica count across all rules.

| Rule | Type | Trigger | Why it exists |
|---|---|---|---|
| `sockets-rule` | KEDA `azure-monitor` | Total `active_sockets` ÷ 80 | Primary signal: steady-state viewer load |
| `http-rule` | Built-in HTTP | 50 concurrent HTTP requests / replica | Backstop for page-load surges before the upgrade-to-WebSocket lands as a counted socket |

`sockets-rule` queries the `active_sockets` custom metric (published by
`app/telemetry.py`) on the App Insights resource via the **Monitoring
Reader** role on the existing user-assigned managed identity (`uami`).
The scale rule and identity reference are wired in `infra/main.bicep`;
the role assignment itself is a **one-time manual step** (see below) —
it isn't declared in Bicep because the GitHub Actions service principal
that runs `azure-infra-deploy` has an RBAC condition that allows only
the AcrPull role id, and widening that condition trades narrow CI scope
for declarative-infra purity.

### One-time Monitoring Reader grant (per environment)

After the first Bicep deploy creates the UAMI and the App Insights
resource, run this once per environment as a tenant admin (or anyone
with `Microsoft.Authorization/roleAssignments/write` on the AI scope):

```bash
# Preprod (swap RG/name suffixes for prod)
ENV="preprod"     # or prod
RG="cts-scoreboard-$ENV"
UAMI_PRINCIPAL_ID=$(az identity show \
  -g "$RG" -n "cts-sb-$ENV-uami" --query principalId -o tsv)
AI_ID=$(az monitor app-insights component show \
  -g "$RG" -a "cts-sb-$ENV-ai" --query id -o tsv)

az role assignment create \
  --assignee-object-id "$UAMI_PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Monitoring Reader" \
  --scope "$AI_ID"
```

`--assignee-object-id` (not `--assignee`) avoids a Graph lookup that
fails on UAMI principals. Propagation takes 2–5 minutes; the KEDA
scaler logs `403` until it lands.

### Why the rule was added

In the 400-browser / 305-socket stress test, the existing HTTP rule
didn't trip until the originating replica's asyncio event loop was
already saturated, producing 100+ second emit fan-out tails. The new
rule reacts to socket count directly (Pi sockets are noise — ≤ 1 per
replica) and asks for ~4 replicas at 305 concurrent viewers (305 / 80).
Combined with `minReplicas: 2` in prod, the first viewers always land
on at least two warm workers.

### Tuning

- **`targetValue: '80'`** — chosen from the stress-test data. Above ~120
  sockets per replica we saw the loop-saturation regime; 80 leaves
  headroom and is still cheap. Do not raise without re-running the
  stress test and checking `emit_fanout_seconds` p95.
- **`metricAggregationType: 'Total'`** — Total sums the per-replica
  cumulative values returned by the OTel UpDownCounter, so KEDA sees
  the true global socket count.
- **`metricAggregationInterval: '0:1:0'`** — matches the OTel SDK's
  default 60s metric-export interval; smaller windows risk one-replica
  windows where data hasn't yet exported.
- **`minReplicas`** — defaults to **2** for prod (was 1) and **0** for
  preprod (unchanged).

### Verification after deploy

1. Wait ~5 minutes after the first revision is healthy, then in
   Application Insights → **Logs**:
   ```kql
   customMetrics
   | where timestamp > ago(15m)
   | where name == "active_sockets"
   | summarize sockets = sum(valueSum) by bin(timestamp, 1m)
   ```
   You should see a non-zero baseline (the Pi connection) within a few
   minutes of any Pi opening a meet.

2. Confirm the KEDA scaler is healthy. In Azure Portal, on the
   Container App → **Revisions and replicas** → select a replica →
   **Console logs**, look for `Found 0 errors` from the KEDA
   azure-monitor scaler. A `403` here means the Monitoring Reader role
   hasn't propagated yet — wait 5 min and re-check.

3. Drive synthetic load (≥ 100 sockets) and confirm replica count
   climbs within ~1–2 minutes (one polling interval + container start).

### Known gotcha — pre-aggregated custom metrics

OTel custom metrics flow to App Insights as both `customMetrics`
(Log Analytics, always available) and as Azure Monitor metrics under
namespace `azure.applicationinsights` (used by KEDA). The Azure Monitor
side requires the App Insights resource to have **custom-metric
dimensions** enabled to surface arbitrary tags, but `active_sockets` is
queried *without* a dimension filter, so we only need the aggregated
metric itself — which is on by default. If a future scaler filters by
`namespace`, you must enable dimensions on the AI resource (Portal →
Application Insights → **Usage and estimated costs** → **Custom
metrics** → "With dimensions").
