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

## Overview of steps

```
1. Get two values from Azure: FQDN and verification ID for each environment
2. Add four DNS records in GoDaddy
3. Wait for DNS to propagate (~5–15 min with low TTLs)
4. Run the infra-deploy workflow for preprod, then prod
5. Verify
```

The Bicep changes (`managedCert` resource + `customDomains` ingress binding)
are already committed to `main.bicep`. **Do not run the infra deploy before the
DNS records are in place** — the managed certificate provisioning depends on the
CNAME being resolvable.

---

## Step 1 — Get the Azure values you need

Run these two commands (substitute your actual resource group names for
`$RG_PREPROD` and `$RG_PROD`):

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

### Records to add

| Type | Name | Value | TTL |
|---|---|---|---|
| CNAME | `scoreboard` | `<PROD_FQDN>` | 600 seconds |
| TXT | `asuid.scoreboard` | `<PROD_VERIFY>` | 3600 seconds |
| CNAME | `scoreboard-pre` | `<PREPROD_FQDN>` | 600 seconds |
| TXT | `asuid.scoreboard-pre` | `<PREPROD_VERIFY>` | 3600 seconds |

Replace `<PROD_FQDN>`, `<PROD_VERIFY>`, etc. with the actual values from
Step 1.

**GoDaddy UI notes:**
- For CNAME records GoDaddy may add `.aquagnomeapps.com` to the Name field
  automatically — enter only the short name (`scoreboard`, not the full FQDN).
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

## Step 4 — Deploy the updated Bicep

Run the **Azure Infrastructure Deploy** workflow twice — once per environment.

### Via GitHub Actions UI

1. Go to **Actions** → **Azure Infrastructure Deploy**.
2. Click **Run workflow**.
3. Set **environment** to `preprod`, leave other fields as-is, click
   **Run workflow**.
4. Wait for it to complete successfully.
5. Repeat with **environment** = `prod`.

### Via CLI

```bash
# preprod
gh workflow run azure-infra-deploy.yml \
  -f environment=preprod

# prod (after preprod succeeds)
gh workflow run azure-infra-deploy.yml \
  -f environment=prod
```

The workflow deploys the Bicep template, which now creates the
`managedCertificate` resource and binds it to the Container App's ingress.
Azure will issue a free TLS certificate (Let's Encrypt backed) and begin
serving traffic at the custom domain. Certificate issuance typically takes
2–5 minutes after the Bicep deployment completes.

---

## Step 5 — Verify

```bash
# Both should return HTTP 200
curl -fsS https://scoreboard.aquagnomeapps.com/healthz
curl -fsS https://scoreboard-pre.aquagnomeapps.com/healthz

# Confirm the TLS cert subject matches
echo | openssl s_client -connect scoreboard.aquagnomeapps.com:443 2>/dev/null \
  | openssl x509 -noout -subject -issuer
```

---

## How routing stays stable across deployments

The custom domain binding lives in the Container App's **ingress configuration**
(a property of the Container App resource), not in any revision. When
deployments run:

- `az containerapp update --image ... --revision-suffix ...` creates a new
  revision but never modifies ingress.
- The infra-deploy workflow pins the running image before re-applying the
  Bicep template, so the custom domain binding is reapplied idempotently each
  time.

The smoke tests in each workflow use `properties.configuration.ingress.fqdn`
(the `*.azurecontainerapps.io` address) — they do not go through GoDaddy,
so DNS changes can never break CI.

---

## Certificate renewal

Azure renews the managed certificate automatically before expiry. No action
required.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Bicep deploy fails with "Custom domain verification failed" | CNAME or TXT record not yet propagated | Wait and re-run the workflow |
| `curl` returns certificate error | Certificate still provisioning | Wait 5 min and retry |
| `dig` returns no result for `asuid.*` TXT | Wrong record name in GoDaddy | Check for extra dots or the `.aquagnomeapps.com` suffix being doubled |
| CNAME resolves to wrong host | Wrong FQDN value pasted | Re-check Step 1 output and update the CNAME |
