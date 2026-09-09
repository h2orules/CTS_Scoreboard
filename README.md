# CTS Scoreboard

Turn a Colorado Time Systems (CTS) timing-console scoreboard feed into a live
swimming scoreboard for a TV, web browser, or OBS browser source. The application
runs locally on a Raspberry Pi and can optionally publish the same meet to an
Azure-hosted relay for remote spectators.

This fork has grown beyond the original serial-to-HTML utility: it includes
meet-file imports, qualifying standards and records, configurable messages and
advertising, a Pi kiosk, and multi-meet cloud hosting. The target appliance is a
**Raspberry Pi 5**. This is an independent project, not an official CTS product;
confirm compatibility with your timing console and wiring before meet day.

## Contents

- [Current capabilities](#current-capabilities)
- [How it fits together](#how-it-fits-together)
- [Pi hardware and serial connection](#pi-hardware-and-serial-connection)
- [Set up the Raspberry Pi](#set-up-the-raspberry-pi)
- [Set up your own Azure relay](#set-up-your-own-azure-relay)
- [Connect the Pi to Azure](#connect-the-pi-to-azure)
- [Development and documentation](#development-and-documentation)
- [Project origins](#project-origins)

## Current capabilities

| Area | What is available |
|---|---|
| Live scoreboard | Event/heat information, lane times and places, running clock, and race-state-driven display updates over WebSockets. |
| Meet data | HyTek/Meet Manager `.hy3` event and entry imports, including swimmer/team and seed-time information; `.st2` qualifying standards and multiple `.rec` record sets. |
| Display controls | Pool/lane settings, display styles, team-name overrides, qualifying/record information, message pages and footer messages. |
| Sponsor content | Image uploads, ordering, enable/disable controls, resizing and timed ad rotation. |
| Local operation | Browser-based settings, serial-port selection, Wi-Fi management through NetworkManager, and a boot-to-scoreboard Chromium kiosk. Azure is not required. |
| Remote viewing | Authenticated Pi-to-Azure publishing, separate meet URLs, friendly meet names, QR-code sharing, reconnect controls and meet lifecycle management. Viewers do not need access to the Pi's LAN. |
| Cloud operations | Container Apps deployments, Redis-backed state and cross-replica Socket.IO fanout, autoscaling, alerts, and version-controlled Azure Monitor workbooks. |
| Development tools | Recorded serial-stream capture/replay, a development-only simulator, Python and JavaScript tests, and a separate stress-test harness. |

Features and imports enrich the scoreboard feed; this application does not
replace the timing console or meet-management software.

## How it fits together

```text
CTS console -------- existing scoreboard cable -------- official scoreboard
                              |
                         receive-only tap
                              |
                         USB RS-232 adapter
                              |
                         Raspberry Pi
                         /           \
               local Flask app       authenticated outbound connection
               /web/home             to optional Azure relay
                   |                         |
             HDMI kiosk / LAN          /m/<meet-id>
             browser / OBS             remote spectators
```

The Pi reads the serial feed and renders the local scoreboard. The Azure
application is a separate FastAPI/Socket.IO relay: it receives meet state,
templates and assets from the Pi, serves remote viewers, and uses Managed Redis
to share state and messages between workers. Configuration and timing-console
access remain on the Pi; Azure is not a remote copy of the Pi's settings UI.

## Pi hardware and serial connection

### Suggested parts list

This is a practical build checklist, not a claim that every listed accessory or
Pi memory size has been qualified. Keep connector choices consistent with the
actual console, existing scoreboard cable and USB adapter.

| Part | Selection notes |
|---|---|
| Raspberry Pi 5 | Target platform. Choose enough RAM for the desktop and Chromium; 4 GB or more is a reasonable starting point, not a measured minimum. |
| Pi 5 power supply | Use a suitable USB-C supply, such as the official 27 W Pi 5 supply, with capacity for USB peripherals. |
| Case and cooling | A ventilated case and Pi 5-compatible cooling for sustained kiosk use. |
| Boot storage | A reliable microSD card, or supported SSD boot setup. A 32 GB or larger card is a practical starting point. |
| Display connection | TV/monitor and a **micro-HDMI to HDMI** cable for the Pi 5. Not needed if all viewing is through other LAN devices. |
| Setup peripherals | Keyboard and mouse for initial desktop setup and kiosk shortcuts. |
| Network | Ethernet or Wi-Fi. Internet is required for Azure publishing, but not for the local scoreboard once installed. |
| USB-to-RS-232 adapter | A Linux-supported adapter with **real RS-232 signalling**, not a USB-to-TTL UART cable. |
| CTS signal tap | A mono 1/4-inch **TS** male-to-two-female Y splitter, plus a DB-9 connector/breakout and insulated strain relief. See below. |

### Receive-only serial tap

For the 1/4-inch mono scoreboard connection described by the original project,
an example splitter is the
[Hosa YPP-111 TS-to-dual-TS-female Y cable](https://www.amazon.com/dp/B000068O53).
Leave one branch carrying the original console-to-scoreboard connection. Use
the other branch for the computer's receive-only tap.

With a conventional PC-style RS-232 adapter:

| Tap conductor | DB-9 connection at the computer/adapter |
|---|---|
| TS tip / center conductor | Pin 2, receive data (RX) |
| TS sleeve / shield | Pin 5, signal ground |
| Everything else | Unconnected; do not connect transmit data or handshake lines |

Use a connector that **mates with your adapter**. Many USB adapters have a male
DB-9, requiring a female DB-9 on the tap; do not choose the gender solely from
a product title. Read the connector's pin numbers carefully: solder-side and
mating-face views are mirrored.

Make and insulate the connections with equipment disconnected, provide strain
relief, and check continuity before connecting the console. Do not connect
RS-232 signals directly to Pi GPIO pins. Verify that the existing scoreboard
still works with the tap attached, and test with your console before relying
on this setup at a meet. The parser opens the serial port at **9600 baud**.

The [protocol references](#project-origins) explain the signal interpretation.
Other connector types or console protocols may need a different interface;
this wiring is not a universal CTS adapter.

## Set up the Raspberry Pi

### 1. Prepare the OS and install the application

The kiosk scripts target **64-bit Raspberry Pi OS Bookworm, desktop edition,
with labwc/Wayland**. A desktop is needed for the HDMI kiosk, not for a
headless server used only by other browsers. Configure your user, networking
and, if wanted, SSH using Raspberry Pi Imager or the OS tools.

Run the following as the regular user who will operate the scoreboard:

```bash
sudo apt update
sudo apt install -y git

# Install uv; see its installation documentation if you prefer another method.
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"

# The supplied kiosk service expects this exact checkout location.
git clone https://github.com/h2orules/CTS_Scoreboard.git ~/scoreboard
cd ~/scoreboard
uv python install 3.13
uv sync

# Set your own web-admin credentials before exposing the app on the LAN.
uv run python set_credentials.py
```

The Pi application requires **Python 3.13 or newer**; `uv` manages the
project's virtual environment. See the
[uv installation guide](https://docs.astral.sh/uv/getting-started/installation/)
for other platforms.

The initial web-login defaults are `admin` / `password` when no credential
store exists. Change them using the command above or the settings page.
Passwords are stored as salted hashes in the local `credentials.json`, not in
the README or tracked configuration.

### 2. Connect and select the serial adapter

Plug in the USB RS-232 adapter. On Raspberry Pi OS, the service user usually
needs membership in `dialout`:

```bash
sudo usermod -aG dialout "$USER"
# Log out and back in for the group change to take effect.

cd ~/scoreboard
uv run python -m serial.tools.list_ports
uv run cts-scoreboard --port /dev/ttyUSB0
```

Replace `/dev/ttyUSB0` with the detected port. Where available, a stable
`/dev/serial/by-id/...` path avoids USB enumeration changes.

Open `http://<pi-ip>:5000/settings`, sign in, and save the serial-port selection
there for subsequent service starts. The `--port` argument selects the input
for that invocation; use the settings page to persist it.

### 3. Configure and view a meet

| Local URL | Purpose |
|---|---|
| `http://<pi-ip>:5000/` | Index of available pages |
| `http://<pi-ip>:5000/web/home` | Main scoreboard; use this as the kiosk or OBS browser-source URL |
| `http://<pi-ip>:5000/settings` | Administration, meet imports and display/network/cloud configuration |

Use **Meet Settings**, **Pool Settings**, and the display sections to configure
the meet. Upload meet entries, standards and records under **Meet Manager
(Hytek/Active) Data**. Message-board, footer and ad controls are on the same
settings page. Example import files are in [`samples/`](samples/).

For normal server operation, stop the development process and run:

```bash
cd ~/scoreboard
./start.sh
```

This uses gunicorn/gevent and binds to `0.0.0.0:5000`. Keep the Pi server at its
default **one worker** because serial input and local scoreboard state live in
that process; Azure's Redis-backed workers are a separate architecture.
Do not run a second copy of the server on the same port.

Use a trusted LAN. Do not port-forward the Pi's administration server to the
Internet; use the Azure relay for public viewing. Back up local settings and
uploaded content, and keep generated credential/token files private.

### 4. Optional: boot directly into the scoreboard

Enable desktop auto-login through `sudo raspi-config`, then:

```bash
cd ~/scoreboard
./pi/scripts/install-kiosk.sh --dry-run
./pi/scripts/install-kiosk.sh
sudo reboot
```

The installer sets up a user systemd service and Chromium kiosk autostart.
`Ctrl+Alt+K` exits the kiosk, `Ctrl+Alt+S` opens settings, and `Ctrl+Alt+R`
reopens the kiosk. The **CTS Scoreboard Kiosk** desktop icon also reopens it.

See [the kiosk setup guide](docs/PI_KIOSK_SETUP.md) for display selection,
service options and troubleshooting. Useful service commands:

```bash
systemctl --user status cts-scoreboard.service
journalctl --user -u cts-scoreboard.service -f
```

To update an existing checkout, stop the service, pull your reviewed changes,
run `uv sync`, and restart the service. Preserve local configuration and
credentials; do not overwrite them with another Pi's files.

## Set up your own Azure relay

**Azure is optional and incurs ongoing charges.** The repository's deployment
files describe the maintainer's installation, not a one-click deployment with
globally unique names for every fork. Use your own subscription, tenant,
resource names, GitHub repository and DNS domain.

### What gets deployed

[`azure/infra/main.bicep`](azure/infra/main.bicep) provisions a separate stack
for `preprod` or `prod`:

| Component | Role |
|---|---|
| Azure Container Apps and environment | FastAPI/Socket.IO relay, HTTPS ingress and autoscaling |
| Azure Container Registry, Basic | Application images |
| Azure Managed Redis, non-HA Balanced B0 | Live meet state and cross-worker/replica pub/sub |
| Storage account, Tables and Blob | Storage resources for meet metadata/snapshots |
| Application Insights and Log Analytics | Telemetry and diagnostics |
| Azure Monitor workbooks, alerts and action group | Performance/viewer dashboards and notifications |
| User-assigned managed identity | Container image pulls and permission to query scaling metrics |

Managed Redis uses **NoCluster**, TLS on port **10000**, access-key
authentication and `VolatileLRU` eviction. Do not substitute the service's
default OSS clustering policy: the state and Socket.IO clients are ordinary
non-cluster Redis clients. The Bicep size choices are B0 and B1.

At the documented West US rates, non-HA B0 is approximately **$11.68/month per
environment** at 730 hours, before credits and other resources. It has no HA
replica or availability SLA. Preprod can scale the app to zero; prod normally
keeps an app replica running. Use Azure budgets and cost alerts rather than
treating the Redis price as the entire hosting bill. See the
[Managed Redis comparison and migration guide](azure/docs/MANAGED_REDIS_MIGRATION.md).

### 1. Prepare your fork, identities and permissions

Install Azure CLI/Bicep, GitHub CLI and `jq` on your workstation. Sign in to
Azure and explicitly select your subscription. You need permission to create
resources, Entra app registrations and the required role assignments, plus
permission to configure GitHub Actions for your fork.

Follow the identity and permission commands in the
[Azure setup guide](azure/docs/AZURE_SETUP.md), substituting your own values.
There are two different kinds of application identity:

- **Relay app registration:** exposes the delegated `Pi.Connect` scope and
  supports the Pi's device-code sign-in. Configure the self-permission and
  consent described in the guide. Its ID is `RELAY_APP_ID`.
- **GitHub deployment identity:** uses OIDC federation, not a client secret.
  Grant resource-group-scoped Contributor/AcrPush and the constrained role
  assignment permission described in the guide. Its app ID is
  `AZURE_CLIENT_ID`, not the relay's client ID.

Adapt the naming expressions in Bicep and corresponding workflow names
(`RG`, `ACR`, `APP`, identities and monitoring resources). ACR, storage and
Managed Redis names must be globally unique. Replace the maintainer's custom
domains in **both** Bicep and `azure-infra-deploy.yml`. Do not reuse the live
`aquagnomeapps.com` DNS names for another installation.

### 2. Configure GitHub Actions

Use this current environment matrix alongside the setup guide's commands:

| GitHub environment | Used by | Setup |
|---|---|---|
| `preprod` | Preprod app/infra deployment and cleanup | Deployment identity with an OIDC subject ending in `environment:preprod` |
| `prod` | Prod infrastructure deployment | Deployment identity with a separate `environment:prod` federated credential; configure production approval protection |
| `production` | Promotion of a tested image to prod | Deployment identity with `environment:production`; configure required reviewers |
| `prod-cleanup` | Unattended scheduled prod cleanup | Separate, narrowly scoped cleanup identity; no reviewer wait, restrict deployment branches to `master` |

Each federated credential's full subject is
`repo:<owner>/<repo>:environment:<environment-name>`, with issuer
`https://token.actions.githubusercontent.com` and audience
`api://AzureADTokenExchange`. The names `prod` and `production` are deliberately
different workflow environments; configure both.

Set these GitHub secrets using the values from your own deployment:

| Secrets | Meaning |
|---|---|
| `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` | Deployment identity and target subscription |
| `RELAY_APP_ID` | Relay/Pi-authentication app registration |
| `RG_PREPROD`, `RG_PROD` | Resource groups |
| `ALERT_EMAIL`, `ALERT_SMS_COUNTRY_CODE`, `ALERT_SMS_PHONE` | Alert recipients required by the current template |

Environment-level secrets override repository-level secrets. In
`prod-cleanup`, set the cleanup identity's own Azure client/tenant/subscription
values and `RG_PROD`. Its only Azure grants should be a custom role allowing
`Microsoft.App/containerApps/read`, `Microsoft.App/containerApps/revisions/read`
and `Microsoft.App/containerApps/revisions/deactivate/action`, scoped to the
prod app, plus `AcrPull` and `AcrDelete` scoped to the prod registry. Create its
own `environment:prod-cleanup` federated credential; do not reuse the broad
deployment identity for this unattended job.

### 3. Bootstrap preprod, then repeat for prod

For a new installation, first deploy without the Container App's
`ingress.customDomains` block in your fork's Bicep. This lets Azure create the
app and return its hostname verification ID before you configure DNS. Restore
the block with your own domain after completing the next step.

The following uses a **real relay image**, so ingress remains on port **8000**
throughout. Set `TENANT_ID`, `RELAY_APP_ID` and the three alert-recipient
variables using the setup guide first. Choose `RG` and `ACR` values matching
your adapted template:

```bash
# From the repository root, on your workstation:
az login
az account set --subscription "<your-subscription-id>"

ENV="preprod"
LOCATION="westus"
RG="<your-preprod-resource-group>"
ACR="<your-globally-unique-preprod-acr-name>"

az group create --name "$RG" --location "$LOCATION"
az acr create --resource-group "$RG" --name "$ACR" --sku Basic
az acr build --registry "$ACR" --image cts-relay:initial ./azure

az deployment group create \
  --resource-group "$RG" \
  --template-file azure/infra/main.bicep \
  --parameters \
    environmentName="$ENV" \
    containerImage="$ACR.azurecr.io/cts-relay:initial" \
    entraTenantId="$TENANT_ID" \
    entraAudience="api://$RELAY_APP_ID" \
    alertEmail="$ALERT_EMAIL" \
    alertSmsCountryCode="$ALERT_SMS_COUNTRY_CODE" \
    alertSmsPhone="$ALERT_SMS_PHONE" \
    redisSkuName=Balanced_B0
```

This avoids the older port-80 placeholder-image bootstrap in the detailed
guide. Allow time for Redis and Container Apps provisioning.

After deployment, grant the app's user-assigned identity **Monitoring Reader**
on its Application Insights resource, using the
[one-time scaling permission instructions](azure/docs/AZURE_SETUP.md#one-time-monitoring-reader-grant-per-environment).
Without that grant, metric-based autoscaling cannot query its data.

### 4. Configure DNS, certificates and ongoing deployments

Use [the custom-domain guide](azure/docs/CUSTOM_DOMAIN_SETUP.md), with your own
domain and app names, to obtain each app's FQDN and
`customDomainVerificationId`. Create the matching CNAME and `asuid` TXT
records, confirm DNS propagation, and restore the custom-domain block in
Bicep. The infra workflow binds the managed certificate after deployment.

Run `azure-infra-deploy` with `environment=preprod`, `mode=what-if`, review
the result, then run `mode=apply`. Repeat bootstrap/DNS/deployment for prod
after preprod works. Check `/healthz`, `/readyz` and the public HTTPS hostname;
readiness includes a Redis connectivity check.

The current workflows then provide:

| Workflow | Behavior |
|---|---|
| `azure-ci` | Lint, type-check reporting, Python tests, Bicep validation and container build for relevant changes |
| `azure-deploy-preprod` | Deploys after successful `azure-ci` on `master`, or on manual dispatch |
| `azure-promote-prod` | Manually promotes a preprod image digest through the `production` approval gate |
| `azure-infra-deploy` | Previews or applies infrastructure changes while preserving the current image |
| `azure-cleanup-revisions` | Weekly revision/image cleanup; manual dry-run also available |

Preprod cleanup retains two active revisions; prod retains four, with
traffic-bearing revisions protected. Image retention keeps five recent
manifests plus any still needed by active revisions. The separately documented
[stress-test workflows](stress-test/README.md) are optional and create
additional billable resources; they are not part of the minimum deployment.

## Connect the Pi to Azure

On the Pi, open **Settings > Cloud Hosting**:

1. Enter your Entra tenant ID and the **relay app's** client ID, not the GitHub
   deployment identity. Configure the preprod/prod relay URLs and optional
   public URLs, then save.
2. Choose preprod first and click **Sign In**. Complete the displayed
   device-code sign-in with an account allowed by your Entra configuration.
3. Confirm the connection status, meet ID and public link. Open the link on
   another device and check that timing updates arrive.
4. Use the friendly-name, QR-code, reconnect, end-meet and rotate-meet-ID
   controls as needed. Switch to your prod configuration after validation.

Public meet pages use `/m/<meet-id>`. The Pi initiates its connection outbound;
you do not need an inbound Internet port forwarded to it. If the uplink fails,
the local scoreboard can continue operating, while remote viewers depend on
the relay connection being restored.

## Development and documentation

### Run and test locally

```bash
# Pi application (repository root)
uv sync
uv run cts-scoreboard --help
uv run pytest

# Development-only simulator; sign in at /test after startup.
SCOREBOARD_MODE=development uv run cts-scoreboard

# Capture and replay a serial stream (run separately, not alongside the service).
uv run cts-scoreboard --port /dev/ttyUSB0 --out recording.dat
uv run cts-scoreboard --in recording.dat --speed 2.0

# JavaScript tests; Node.js/npm are needed for tests, not to serve the Pi UI.
npm ci
npm test
```

The `/test` simulator is disabled outside development mode. `--speed` controls
recording playback, not the serial baud rate.

The Azure application has its own project and environment (Python 3.12+):

```bash
cd azure
uv sync --extra dev
uv run ruff check app tests
uv run mypy app
uv run pytest tests/unit tests/integration -q
```

Normal Azure tests use fakeredis; the opt-in real Redis contract is described
in the Managed Redis guide. The current strict mypy baseline has known errors
and is advisory in CI; do not assume a green workflow means it is type-clean.

### Documentation map

| Guide | Contents |
|---|---|
| [Pi kiosk setup](docs/PI_KIOSK_SETUP.md) | Desktop autostart, systemd service, shortcuts and troubleshooting |
| [Azure setup](azure/docs/AZURE_SETUP.md) | Entra registration, RBAC, secrets, provisioning and scaling details |
| [Custom domains](azure/docs/CUSTOM_DOMAIN_SETUP.md) | DNS ownership records and managed HTTPS certificates |
| [Managed Redis](azure/docs/MANAGED_REDIS_MIGRATION.md) | B0/B1 costs, client compatibility and migration/rollback precautions |
| [Performance dashboard](azure/docs/DASHBOARD.md) | Redis, fanout and application performance queries |
| [Viewer engagement](azure/docs/ENGAGEMENT-DASHBOARD.md) | Meet/viewer usage analysis |
| [Analytics notebooks](azure/notebooks/README.md) | Querying and exploring telemetry |
| [Stress testing](stress-test/README.md) | Optional load-generation infrastructure and procedures |
| [HyTek standards format](docs/HYTEK_ST2_FORMAT.md) | `.st2` parser/format notes |
| [HyTek records format](docs/HYTEK_REC_FORMAT.md) | `.rec` parser/format notes |

The deployable workbook definitions live in
[`azure/infra/workbooks/`](azure/infra/workbooks/). The subscription-migration
guide and planning documents are historical/special-purpose references, not
prerequisites for a fresh Pi installation.

## Project origins

Forked from [STU940652/CTS_Scoreboard](https://github.com/STU940652/CTS_Scoreboard),
with Raspberry Pi 5 support and extensive local/cloud scoreboard additions.
Protocol interpretation builds on
[hwbrill/vsCTS](https://github.com/hwbrill/vsCTS/blob/master/README.md) and
[Marco's Colorado timing-console protocol notes](https://marcoscorner.walther-family.org/2015/07/colorado-timing-console-scoreboard-protocol/).

CTS, HyTek and other product names belong to their respective owners. This
project is not affiliated with or endorsed by those vendors.
