# Subscription Migration — CTS Scoreboard Relay

Move the entire Azure relay (preprod **and** prod) from the current
subscription to the **Visual Studio Enterprise Subscription** to take
advantage of the $150/mo free credits.

| Item | Value |
|---|---|
| **Target subscription name** | `Visual Studio Enterprise Subscription` |
| **Target subscription ID** | `ab15049e-2b8d-412d-a516-cb41fe181cb9` |
| **Entra tenant (unchanged)** | `e9a5beec-146b-4937-b9b9-890e415b9d62` |
| Environments | preprod + prod |
| Data to migrate | `meets` Table + `meet-snapshots` Blob container |
| Custom domains | `scoreboard.aquagnomeapps.com` (prod), `scoreboard-pre.aquagnomeapps.com` (preprod) — DNS cutover required |

> Read the whole document once before starting. Then **do preprod
> end-to-end first** to rehearse the procedure, and only then do prod.

---

## Why not just use the "Move" button?

Azure resource move (and Azure Resource Mover) **does not support** several
resource types in this stack:

- `Microsoft.App/managedEnvironments` and `Microsoft.App/containerApps`
  (Container Apps cannot be moved between subscriptions at all).
- Container Apps **managed certificates** (the free TLS cert behind your
  custom domains).
- A **user-assigned managed identity** (`*-uami`) that has live **role
  assignments** and is **referenced by** the Container App — which is
  exactly the error you hit. RG move is all-or-nothing, so one unmovable
  member fails the entire request.

Because the whole stack is already defined in
[infra/main.bicep](../infra/main.bicep) and driven by GitHub Actions, the
correct, supported approach is a **clean redeploy into the new
subscription**, migrating only the durable data (Storage). Redis holds only
ephemeral live-meet cache state and is **not** migrated. This is faster and
safer than fighting the move API.

### What stays the same (because the tenant is unchanged)

You do **not** recreate any of these — they live at the tenant level and are
subscription-independent:

- The relay **app registration** (`entraAudience` / `RELAY_APP_ID`) and its
  `Pi.Connect` scope grant.
- The **GitHub Actions** app registration + its **OIDC federated
  credentials** (`repo:h2orules/CTS_Scoreboard:environment:preprod|production`).
- **The Pi needs no reconfiguration.** Same tenant ID, same audience, and —
  after the DNS cutover — the same `scoreboard.aquagnomeapps.com` URL. It
  re-authenticates and reconnects on its own.

### What changes

- New **role assignments** for the GitHub Actions SP, scoped to the new
  subscription's resource groups.
- The single GitHub secret `AZURE_SUBSCRIPTION_ID`.
- A brand-new **managed TLS certificate** is issued automatically during the
  DNS cutover (certs can't be migrated; this is expected).

Resource **names are kept identical** (`ctssbpreprodacr`,
`cts-sb-prod-app`, `cts-scoreboard-preprod`, …) so the workflows, the
`customDomain` logic in Bicep, and every other GitHub secret stay untouched.
ACR / Storage / Redis names are **globally unique**, so the old resources
must be deleted before the new ones are created — hence the
stage-data → delete-old → redeploy ordering below.

---

## 0. Prerequisites (run on your workstation)

```bash
az --version            # >= 2.60
az bicep version
gh --version
python3 --version       # for the SAS-expiry helper + Table-copy script (step 5)

# azcopy is needed only if Blob storage actually has data to migrate.
# macOS:   brew install azcopy
# Linux:   https://aka.ms/downloadazcopy  (or your package manager)
# If your relay's Storage layer isn't in use yet (empty container/table),
# you can skip installing it — the staging step prints "(no blobs ...)".
azcopy --version || echo "azcopy not installed (only needed if blobs exist)"

az login                # sign in to the tenant that owns BOTH subscriptions
```

Set the working variables once. Fill in `OLD_SUB_ID` from
`az account list -o table`.

```bash
export TENANT_ID="e9a5beec-146b-4937-b9b9-890e415b9d62"
export NEW_SUB_ID="ab15049e-2b8d-412d-a516-cb41fe181cb9"
export OLD_SUB_ID="<your-current-subscription-id>"   # az account list -o table

# These are unchanged from AZURE_SETUP.md — names are reused as-is.
export RG_PREPROD="cts-scoreboard-preprod"
export RG_PROD="cts-scoreboard-prod"
export LOCATION="westus2"

# Reused tenant-level identities (do NOT recreate — same tenant).
export GH_APP_ID="<AZURE_CLIENT_ID secret value>"     # gh secret / az ad app list
export RELAY_APP_ID="<RELAY_APP_ID secret value>"

# Alert recipients (same as before; needed for the Bicep deploys).
export ALERT_EMAIL="<your.email@example.com>"
export ALERT_SMS_COUNTRY_CODE="1"
export ALERT_SMS_PHONE="<10-digit-number-no-formatting>"
```

Confirm both subscriptions are visible and in the same tenant:

```bash
az account list --query "[?id=='$OLD_SUB_ID' || id=='$NEW_SUB_ID'].{name:name,id:id,tenantId:tenantId}" -o table
# Both rows must show tenantId == $TENANT_ID
```

---

## 1. Lower DNS TTLs ahead of time (do this first, a day before if possible)

The cutover swaps the GoDaddy records to the new Container Apps. Lowering the
TTL now shortens the propagation window later.

In **GoDaddy → aquagnomeapps.com → DNS**, set the TTL to **600 seconds** on:

- `CNAME scoreboard`
- `CNAME scoreboard-pre`
- `TXT asuid.scoreboard`
- `TXT asuid.scoreboard-pre`

(Don't change the values yet — only the TTL.) Wait at least the old TTL
duration before starting the cutover.

---

## 2. Stage the durable data locally (NOTHING is deleted yet)

This pulls the Storage contents to your workstation so they survive the
old-subscription teardown. Run for **both** environments.

> **Preflight — is there anything to migrate?** The relay's Storage layer
> may not be in use yet (`app/storage.py` is still a stub). Check first; if
> both come back empty for both environments, **skip this entire section
> and step 5** and go straight to step 3.
>
> ```bash
> az account set --subscription "$OLD_SUB_ID"
> for pair in "$RG_PREPROD ctssbpreprodst" "$RG_PROD ctssbprodst"; do
>   set -- $pair; RG=$1; STG=$2
>   echo "== $STG =="
>   az storage blob list --account-name "$STG" --container-name meet-snapshots \
>     --auth-mode key --num-results 1 -o tsv 2>/dev/null | head -1 || echo "  (no blobs)"
>   az storage entity query --account-name "$STG" --table-name meets \
>     --auth-mode key --num-results 1 -o tsv 2>/dev/null | head -1 || echo "  (no table rows)"
> done
> ```

```bash
az account set --subscription "$OLD_SUB_ID"

mkdir -p ~/sb-migrate/preprod/blobs ~/sb-migrate/prod/blobs

# ---- per-environment: export blobs + tables ----
# NOTE: `azcopy` below assumes the binary is on PATH. If you downloaded it to
# the current directory, change `azcopy` to `./azcopy` (or move it onto PATH).
stage_out () {
  local ENV="$1" RG="$2" STG="$3" OUT="$HOME/sb-migrate/$ENV"
  local AZCOPY="${AZCOPY:-azcopy}"   # override with AZCOPY=./azcopy if needed

  # Portable UTC "now + 2h" expiry, inlined so a partial paste can't leave it
  # empty (GNU `date -d` and BSD/macOS `date -v` disagree; Python is uniform).
  local KEY SAS EXPIRY
  KEY=$(az storage account keys list -g "$RG" -n "$STG" --query "[0].value" -o tsv)
  EXPIRY=$(python3 -c "import datetime;print((datetime.datetime.utcnow()+datetime.timedelta(hours=2)).strftime('%Y-%m-%dT%H:%MZ'))")

  # Blob container 'meet-snapshots' -> local. Skipped if azcopy isn't found.
  if command -v "$AZCOPY" >/dev/null 2>&1; then
    SAS=$(az storage container generate-sas --account-name "$STG" --account-key "$KEY" \
          --name meet-snapshots --permissions rl --expiry "$EXPIRY" -o tsv)
    "$AZCOPY" copy \
      "https://$STG.blob.core.windows.net/meet-snapshots?$SAS" \
      "$OUT/blobs" --recursive || echo "  (no blobs / empty container — OK)"
  else
    echo "  (azcopy not on PATH — skipping blob export for $ENV; set AZCOPY=./azcopy if you have a local copy)"
  fi

  # Table 'meets' -> local JSON (AzCopy v10 cannot do tables; use the script).
  # Note: the venv created below provides the python with azure-data-tables.
  CONN=$(az storage account show-connection-string -g "$RG" -n "$STG" -o tsv)
  ~/sb-migrate/.venv/bin/python "$HOME/sb-migrate/copy_table.py" export "$CONN" meets "$OUT/meets.json" \
    || echo "  (table empty / not yet in use — OK)"
}
```

Create the tiny Table copy helper (AzCopy v10 dropped Table support, so we
use the Azure Tables SDK):

```bash
cat > ~/sb-migrate/copy_table.py <<'PY'
"""Export/import an Azure Storage Table to/from a local JSON file."""
import json, sys
from azure.data.tables import TableClient, UpdateMode

def export(conn, table, path):
    tc = TableClient.from_connection_string(conn, table_name=table)
    rows = [dict(e) for e in tc.list_entities()]
    with open(path, "w") as f:
        json.dump(rows, f, default=str)
    print(f"exported {len(rows)} entities -> {path}")

def import_(conn, table, path):
    tc = TableClient.from_connection_string(conn, table_name=table)
    try:
        tc.create_table()
    except Exception:
        pass
    with open(path) as f:
        rows = json.load(f)
    for e in rows:
        tc.upsert_entity(e, mode=UpdateMode.REPLACE)
    print(f"imported {len(rows)} entities -> {table}")

if __name__ == "__main__":
    mode, conn, table, path = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    (export if mode == "export" else import_)(conn, table, path)
PY

# One-off venv for the script (must exist BEFORE running stage_out).
python3 -m venv ~/sb-migrate/.venv
~/sb-migrate/.venv/bin/pip install --quiet azure-data-tables
```

Run the export for both environments (storage account names from the Bicep
naming convention — `ctssb<env>st`):

```bash
stage_out preprod "$RG_PREPROD" ctssbpreprodst
stage_out prod    "$RG_PROD"    ctssbprodst

ls -R ~/sb-migrate            # sanity check the exported files exist
```

> If the relay's Storage layer hasn't been used yet (the table/container are
> empty), the export simply produces nothing — that's fine, you can skip the
> import in step 5.

---

## 3. Prepare the NEW subscription

```bash
az account set --subscription "$NEW_SUB_ID"

# 3a. Register resource providers (fresh subscriptions often lack these).
for NS in Microsoft.App Microsoft.ContainerRegistry Microsoft.Cache \
          Microsoft.OperationalInsights Microsoft.Insights \
          Microsoft.ManagedIdentity Microsoft.Storage \
          Microsoft.AlertsManagement; do
  az provider register --namespace "$NS"
done
# Wait until all show 'Registered':
for NS in Microsoft.App Microsoft.ContainerRegistry Microsoft.Cache \
          Microsoft.OperationalInsights Microsoft.Insights \
          Microsoft.ManagedIdentity Microsoft.Storage Microsoft.AlertsManagement; do
  printf '%-38s %s\n' "$NS" "$(az provider show -n $NS --query registrationState -o tsv)"
done

# 3b. Resource groups (same names, new subscription).
az group create -n "$RG_PREPROD" -l "$LOCATION"
az group create -n "$RG_PROD"    -l "$LOCATION"
```

### 3c. Re-grant the GitHub Actions SP on the new resource groups

These are the same role assignments from `AZURE_SETUP.md` §4, but scoped to
the new subscription. The federated credentials themselves are unchanged.

```bash
ACR_PULL_ROLE_ID="7f951dda-4ed3-4680-a7ca-43fe172d538d"
RBAC_ADMIN_ROLE_ID="f58310d9-a9f6-439a-9e8d-f62e7b41a168"
RBAC_CONDITION="((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {$ACR_PULL_ROLE_ID})) AND ((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})) OR (@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {$ACR_PULL_ROLE_ID}))"

for RG in "$RG_PREPROD" "$RG_PROD"; do
  SCOPE="/subscriptions/$NEW_SUB_ID/resourceGroups/$RG"
  az role assignment create --assignee "$GH_APP_ID" --role Contributor --scope "$SCOPE"
  az role assignment create --assignee "$GH_APP_ID" --role AcrPush     --scope "$SCOPE"
  az role assignment create --assignee "$GH_APP_ID" --role "$RBAC_ADMIN_ROLE_ID" \
    --scope "$SCOPE" \
    --description "Allow GH Actions to create AcrPull role assignments only" \
    --condition "$RBAC_CONDITION" --condition-version "2.0"
done
```

> The relay app registration and its `Pi.Connect` grant are tenant-level and
> already exist — do **not** recreate them.

---

## 4. Tear down the OLD environment, then redeploy in the new subscription

> **This is the start of the planned downtime window.** Do it when no meet
> is running. Do preprod first, validate, then repeat for prod.

### 4a. Delete the old resource group (frees the global ACR/Storage/Redis names)

```bash
az account set --subscription "$OLD_SUB_ID"
az group delete -n "$RG_PREPROD" --yes        # then later: $RG_PROD
```

Wait for deletion to finish before continuing (global names like
`ctssbpreprodacr` / `ctssbpreprodst` aren't released until it completes):

```bash
az group exists -n "$RG_PREPROD"    # must print 'false'
```

### 4b. Bootstrap the ACR in the new subscription

```bash
az account set --subscription "$NEW_SUB_ID"

ACR_PREPROD="ctssbpreprodacr"
az acr create -g "$RG_PREPROD" -n "$ACR_PREPROD" --sku Basic
az acr import --name "$ACR_PREPROD" \
  --source mcr.microsoft.com/azuredocs/aci-helloworld:latest \
  --image cts-relay:bootstrap
```

### 4c. First Bicep deploy (bootstrap image, port 80)

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

### 4d. One-time Monitoring Reader grant for the KEDA scaler (per env)

```bash
ENV="preprod"; RG="$RG_PREPROD"
UAMI_PRINCIPAL_ID=$(az identity show -g "$RG" -n "cts-sb-$ENV-uami" --query principalId -o tsv)
AI_ID=$(az monitor app-insights component show -g "$RG" -a "cts-sb-$ENV-ai" --query id -o tsv)
az role assignment create \
  --assignee-object-id "$UAMI_PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Monitoring Reader" \
  --scope "$AI_ID"
```

---

## 5. Restore the staged data into the new Storage account

```bash
# If both environments' storage was empty in the step-2 preflight, skip this
# whole step — there is nothing to restore.

restore_in () {
  local ENV="$1" RG="$2" STG="$3" OUT="$HOME/sb-migrate/$ENV"
  local AZCOPY="${AZCOPY:-azcopy}"
  local KEY SAS EXPIRY CONN
  KEY=$(az storage account keys list -g "$RG" -n "$STG" --query "[0].value" -o tsv)

  # Blobs (skip if nothing was staged or azcopy is missing).
  if [ -n "$(ls -A "$OUT/blobs" 2>/dev/null)" ] && command -v "$AZCOPY" >/dev/null 2>&1; then
    EXPIRY=$(python3 -c "import datetime;print((datetime.datetime.utcnow()+datetime.timedelta(hours=2)).strftime('%Y-%m-%dT%H:%MZ'))")
    SAS=$(az storage container generate-sas --account-name "$STG" --account-key "$KEY" \
          --name meet-snapshots --permissions rwl --expiry "$EXPIRY" -o tsv)
    "$AZCOPY" copy "$OUT/blobs/*" \
      "https://$STG.blob.core.windows.net/meet-snapshots?$SAS" --recursive
  fi

  # Table (skip if no export file).
  if [ -f "$OUT/meets.json" ]; then
    CONN=$(az storage account show-connection-string -g "$RG" -n "$STG" -o tsv)
    ~/sb-migrate/.venv/bin/python ~/sb-migrate/copy_table.py import "$CONN" meets "$OUT/meets.json"
  fi
}

restore_in preprod "$RG_PREPROD" ctssbpreprodst
# (prod later)
```

---

## 6. Point GitHub at the new subscription and deploy the real image

The only secret that changes is the subscription ID. RG names, tenant,
client ID, relay app ID, and alert secrets are unchanged.

```bash
gh repo set-default h2orules/CTS_Scoreboard
gh secret set AZURE_SUBSCRIPTION_ID --body "$NEW_SUB_ID"
```

Then deploy the real container image:

1. **Actions → `azure-deploy-preprod` → Run workflow.** It builds, pushes to
   `ctssbpreprodacr`, and rolls the Container App to the real image.
2. Smoke-test the default URL printed in the workflow output:
   ```bash
   PREPROD_FQDN=$(az containerapp show -g "$RG_PREPROD" -n cts-sb-preprod-app \
     --query properties.configuration.ingress.fqdn -o tsv)
   curl -fsS "https://$PREPROD_FQDN/healthz" && echo OK
   ```

> After this real deploy, the ingress is back on port 8000 automatically
> (the workflow doesn't pass `targetPort`). No need to re-run the bootstrap
> Bicep with `targetPort=80`.

---

## 7. Cut over the custom domain DNS

The new Container App has a **new** `customDomainVerificationId` and a new
FQDN, so both the CNAME and the `asuid` TXT records must be updated.

```bash
# preprod values:
PREPROD_FQDN=$(az containerapp show -g "$RG_PREPROD" -n cts-sb-preprod-app \
  --query properties.configuration.ingress.fqdn -o tsv)
PREPROD_VERIFY=$(az containerapp show -g "$RG_PREPROD" -n cts-sb-preprod-app \
  --query properties.customDomainVerificationId -o tsv)
echo "scoreboard-pre  CNAME -> $PREPROD_FQDN"
echo "asuid.scoreboard-pre TXT -> $PREPROD_VERIFY"
```

In **GoDaddy**, update (don't duplicate) these records to the new values:

| Type | Name | New value | TTL |
|---|---|---|---|
| CNAME | `scoreboard-pre` | `<PREPROD_FQDN>` | 600 |
| TXT | `asuid.scoreboard-pre` | `<PREPROD_VERIFY>` | 3600 |

Wait for propagation:

```bash
dig +short CNAME scoreboard-pre.aquagnomeapps.com   # -> new FQDN
dig +short TXT   asuid.scoreboard-pre.aquagnomeapps.com  # -> new verify id
```

Then bind the managed cert by running the infra workflow (it issues a fresh
Let's Encrypt-backed cert and sets `SniEnabled`):

```bash
gh workflow run azure-infra-deploy.yml -f environment=preprod -f mode=apply
```

Verify the custom domain serves HTTPS:

```bash
curl -fsS https://scoreboard-pre.aquagnomeapps.com/healthz && echo OK
```

---

## 8. Repeat for prod

Run the same sequence for prod, substituting the prod names/values:

1. **Stage data** (already done in step 2 for prod).
2. **§3** is shared — already done (RG + role grants created for prod too).
3. `az group delete -n "$RG_PROD" --yes` (old sub) → wait for `az group exists` = false.
4. Bootstrap ACR `ctssbprodacr`, first Bicep deploy with
   `environmentName=prod` and `containerImage=$ACR_PROD.azurecr.io/cts-relay:bootstrap`.
5. Monitoring Reader grant with `ENV=prod`, `RG=$RG_PROD`.
6. `restore_in prod "$RG_PROD" ctssbprodst`.
7. Deploy the real image: **Actions → `azure-promote-prod`** (uses the digest
   from the preprod run; the `production` environment requires your approval).
8. DNS cutover for `scoreboard` / `asuid.scoreboard` to the prod app's FQDN
   and verification id, then
   `gh workflow run azure-infra-deploy.yml -f environment=prod -f mode=apply`
   to bind the cert.

```bash
# prod custom-domain values:
PROD_FQDN=$(az containerapp show -g "$RG_PROD" -n cts-sb-prod-app \
  --query properties.configuration.ingress.fqdn -o tsv)
PROD_VERIFY=$(az containerapp show -g "$RG_PROD" -n cts-sb-prod-app \
  --query properties.customDomainVerificationId -o tsv)
echo "scoreboard  CNAME -> $PROD_FQDN"
echo "asuid.scoreboard TXT -> $PROD_VERIFY"

curl -fsS https://scoreboard.aquagnomeapps.com/healthz && echo OK
```

---

## 9. The Pi — verify (no changes needed)

Because the tenant ID, audience (`api://$RELAY_APP_ID`), and the public URL
(`scoreboard.aquagnomeapps.com`) are all unchanged, the Pi requires **no
reconfiguration**. Confirm it reconnects after the prod cutover:

- On the Pi's relay status page (or its logs), confirm a fresh successful
  `/pi` connection.
- In the new subscription's Application Insights, confirm the
  `pi_connections` metric goes to 1 when the Pi is connected:
  ```kql
  customMetrics | where timestamp > ago(15m) | where name == "pi_connections"
  | summarize sum(value) by bin(timestamp, 1m) | render timechart
  ```

If the Pi was signed in with a cached token, no device-code re-auth is even
required. If it does prompt, sign in as normal — same tenant, same app.

---

## 10. Post-migration cleanup & verification

```bash
# Confirm the old subscription has no remaining scoreboard resource groups.
az account set --subscription "$OLD_SUB_ID"
az group list --query "[?starts_with(name,'cts-scoreboard')].name" -o tsv   # expect empty

# Remove the now-stale GH-Actions role assignments in the OLD subscription
# (optional — they vanished with the RGs, but the SP-level listing is tidier).
az role assignment list --assignee "$GH_APP_ID" \
  --subscription "$OLD_SUB_ID" -o table

# Local staging data can be deleted once both envs are verified.
# rm -rf ~/sb-migrate
```

Update the running config in the repo so docs/tools point at the new
subscription if any of these are tracked locally:

- `azure/azure_settings.json` URLs are blank by default — populate only if
  you use them locally.
- There is **no** subscription ID hardcoded in `infra/main.bicep` or the
  workflows; they read `subscription()` / the `AZURE_SUBSCRIPTION_ID` secret,
  so no source edits are required.

---

## Rollback (if the new deploy misbehaves before DNS cutover)

Until you change the GoDaddy records in step 7/8, the **old** environment is
deleted but DNS still resolves to nothing-new — so there is a downtime window
rather than a clean A/B. To keep a true rollback path for **prod**, see the
zero-downtime variant below.

### Optional: zero-downtime prod cutover (advanced)

If prod cannot tolerate the delete-first downtime window, deploy the new prod
stack **side-by-side** under temporary, globally-unique names instead of
deleting first:

1. In a branch, give the new deployment distinct names by adding a salt to
   the Bicep `prefix` (e.g. `var prefix = 'cts-sb-${environmentName}2'`) and
   update the matching `ACR:` / `APP:` values in the deploy workflows.
2. Stand the new stack up fully, migrate data, and validate on its default
   `*.azurecontainerapps.io` URL while the old prod still serves the custom
   domain.
3. Cut the GoDaddy `scoreboard` CNAME + `asuid` TXT to the new app, bind the
   cert, verify.
4. Delete the old prod RG.

This avoids any downtime but leaves the prod resources with the `…2` suffix
permanently (Azure can't rename resources). For a hobby-scale prod the
simpler delete-first path in §8, run during a quiet window, is usually fine.

---

## Cost note

The new stack is identical in shape, so the idle baseline is the same
(~$17/mo: Redis Basic C0 + Storage + Container Apps min-replicas), now
covered by the $150/mo Visual Studio Enterprise credit. Redis Basic C0 is
the single largest idle line item; if you want to drive the bill toward $0,
consider dropping Redis and running prod at `maxReplicas: 1` (single replica
removes the need for cross-replica Socket.IO fanout) — but that's a separate
architecture change, out of scope for this migration.
