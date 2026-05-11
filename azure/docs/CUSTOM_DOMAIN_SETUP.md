# Custom Domain Setup — scoreboard.aquagnomeapps.com

Maps the public-facing subdomains to each environment:

| Environment | URL |
|---|---|
| prod | https://scoreboard.aquagnomeapps.com |
| preprod | https://scoreboard-pre.aquagnomeapps.com |

Custom domain bindings live on the Container App resource, not on individual
revisions. Once this setup is complete, every future deployment (new image,
`az containerapp update`, or infra redeploy) automatically serves through these
URLs — no per-deploy DNS work required.

---

## How the cert gets created (and why it's not in Bicep)

Azure requires a hostname to be **registered on a Container App first** before
it can issue a managed certificate for it — but the cert's resource ID is
needed in the Container App ingress to activate HTTPS. That circular dependency
means a single Bicep deployment cannot do both steps.

The solution used here:

1. Bicep registers the custom hostname with `bindingType: 'Disabled'` (no cert
   yet — this is idempotent and harmless).
2. Immediately after the Bicep deploy, the infra-deploy workflow runs
   `az containerapp hostname bind --validation-method CNAME`, which issues a
   free managed certificate (Let's Encrypt-backed, auto-renewed by Azure) and
   sets `bindingType` to `SniEnabled`. This command is idempotent — on
   subsequent infra deploys it reattaches the existing cert in seconds.

App-image deploys (`az containerapp update`) only create new revisions and
never touch ingress, so those never disturb the cert binding.

---

## Overview of steps

```
1. Get two values from Azure for each environment: FQDN and verification ID
2. Add four DNS records in GoDaddy
3. Wait for DNS to propagate (~5–15 min with low TTLs)
4. Run the infra-deploy workflow (mode: apply) for preprod, then prod
5. Verify
```

**Do not run the infra deploy before the DNS records are in place.** The
`az containerapp hostname bind` step in the workflow will fail if the CNAME
or TXT record is missing.

---

## Step 1 — Get the Azure values you need

Run these commands (substitute your actual resource group names):

```bash
# ---- preprod ----
PREPROD_FQDN=$(az containerapp show \
  -g "$RG_PREPROD" -n cts-sb-preprod-app \
  --query properties.configuration.ingress.fqdn -o tsv)

PREPROD_VERIFY=$(az containerapp show \
  -g "$RG_PREPROD" -n cts-sb-preprod-app \
  --query properties.customDomainVerificationId -o tsv)

echo "preprod FQDN:    $PREPROD_FQDN"
echo "preprod verify:  $PREPROD_VERIFY"

# ---- prod ----
PROD_FQDN=$(az containerapp show \
  -g "$RG_PROD" -n cts-sb-prod-app \
  --query properties.configuration.ingress.fqdn -o tsv)

PROD_VERIFY=$(az containerapp show \
  -g "$RG_PROD" -n cts-sb-prod-app \
  --query properties.customDomainVerificationId -o tsv)

echo "prod FQDN:    $PROD_FQDN"
echo "prod verify:  $PROD_VERIFY"
```

You will end up with four values that look roughly like:

| Variable | Example value |
|---|---|
| `PREPROD_FQDN` | `cts-sb-preprod-app.westus2.azurecontainerapps.io` |
| `PREPROD_VERIFY` | `A1B2C3D4E5F6...` (long hex string) |
| `PROD_FQDN` | `cts-sb-prod-app.westus2.azurecontainerapps.io` |
| `PROD_VERIFY` | `7G8H9I0J1K2L...` (long hex string) |

Keep these handy — you need all four for the GoDaddy records.

---

## Step 2 — Add DNS records in GoDaddy

1. Log in to [GoDaddy](https://dcc.godaddy.com) and open **My Products**.
2. Next to **aquagnomeapps.com**, click **DNS** → **Manage**.
3. Add the following four records (delete any existing CNAME for `scoreboard`
   or `scoreboard-pre` if present):

| Type | Name | Value | TTL |
|---|---|---|---|
| CNAME | `scoreboard` | `<PROD_FQDN>` | 600 seconds |
| TXT | `asuid.scoreboard` | `<PROD_VERIFY>` | 3600 seconds |
| CNAME | `scoreboard-pre` | `<PREPROD_FQDN>` | 600 seconds |
| TXT | `asuid.scoreboard-pre` | `<PREPROD_VERIFY>` | 3600 seconds |

Replace `<PROD_FQDN>`, `<PROD_VERIFY>`, etc. with the actual values from
Step 1.

**GoDaddy UI notes:**
- For CNAME records, GoDaddy may append `.aquagnomeapps.com` to the Name field
  automatically — enter only the short label (`scoreboard`, not the full FQDN).
- GoDaddy's minimum TTL in the UI is 600 seconds (10 minutes); that is fine.
- The `asuid.*` TXT records are Azure's domain ownership proof — they must
  match the `customDomainVerificationId` exactly, including capitalisation.

---

## Step 3 — Wait for DNS propagation

Check that both CNAMEs resolve before proceeding:

```bash
# Should return the Azure Container App FQDN
dig +short CNAME scoreboard.aquagnomeapps.com
dig +short CNAME scoreboard-pre.aquagnomeapps.com

# Should return the verification strings
dig +short TXT asuid.scoreboard.aquagnomeapps.com
dig +short TXT asuid.scoreboard-pre.aquagnomeapps.com
```

GoDaddy typically propagates within 5–15 minutes when TTL is 600s. You can
also use https://dnschecker.org to confirm global propagation.

---

## Step 4 — Run the infra-deploy workflow (mode: apply)

Run the **Azure Infrastructure Deploy** workflow for each environment, with
**mode: apply** (not what-if).

### Via GitHub Actions UI

1. Go to **Actions** → **Azure Infrastructure Deploy** → **Run workflow**.
2. Set **environment** to `preprod`, **mode** to `apply`, click **Run workflow**.
3. Watch the **Bind managed certificate** step — it calls
   `az containerapp hostname bind` and should complete in under 60 seconds.
4. Wait for the workflow to complete successfully.
5. Repeat with **environment** = `prod`.

### Via CLI

```bash
gh workflow run azure-infra-deploy.yml -f environment=preprod -f mode=apply
# wait for it to succeed, then:
gh workflow run azure-infra-deploy.yml -f environment=prod    -f mode=apply
```

---

## Step 5 — Verify

```bash
# Both should return HTTP 200
curl -fsS https://scoreboard.aquagnomeapps.com/healthz
curl -fsS https://scoreboard-pre.aquagnomeapps.com/healthz

# Confirm the TLS cert subject
echo | openssl s_client -connect scoreboard.aquagnomeapps.com:443 2>/dev/null \
  | openssl x509 -noout -subject -issuer
```

---

## How routing stays stable across deployments

| Deployment type | Touches ingress? | Custom domain affected? |
|---|---|---|
| `az containerapp update` (image deploy) | No | No — cert binding unchanged |
| `az deployment group create` (infra deploy) | Yes — resets hostname to `Disabled` | Re-bound immediately by workflow's `hostname bind` step |

The smoke tests in each workflow use the `*.azurecontainerapps.io` FQDN
directly, so they never depend on GoDaddy DNS.

---

## Certificate renewal

Azure renews the managed certificate automatically before expiry. No action
required.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `hostname bind` step fails: "Custom hostname not found" | Bicep didn't register the hostname (unexpected) | Re-run the infra deploy; check that `customDomains` with `Disabled` is in main.bicep |
| `hostname bind` step fails: "CNAME validation failed" | CNAME or TXT not yet propagated | Wait and re-run the workflow |
| `curl` returns certificate error after workflow | Cert still provisioning | Wait 2–5 min and retry |
| `dig` returns nothing for `asuid.*` | Wrong record name in GoDaddy | Check for doubled `.aquagnomeapps.com` suffix |
| CNAME resolves to wrong host | Wrong FQDN pasted | Re-check Step 1 output and update the CNAME record |
