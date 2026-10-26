# Deploying the Jolt backend (dev and prod)

Everything in Azure is described in `infra/` (Bicep) and created by one command,
`scripts/deploy-infra.sh`. After that, every push to `main` builds, deploys and
smoke-tests `dev` automatically (`.github/workflows/deploy-dev.yml`), and a `v*`
tag (or a manual run) promotes that same image to `prod` after an approval
(`.github/workflows/deploy-prod.yml`). This page lists every manual step and
every value the owner has to provide; there are no secrets to paste anywhere.

`dev` and `prod` come from the same template (`infra/main.bicep`) with their own
parameters file (`infra/main.dev.bicepparam`, `infra/main.prod.bicepparam`) and
resource group (`jolt-dev`, `jolt-prod`). The sections up to 5 walk through dev;
[Production](#production) lists what differs.

- [What gets created](#what-gets-created)
- [Owner checklist](#owner-checklist) (the manual steps, in order)
- [1. Prerequisites](#1-prerequisites)
- [2. Review the parameters](#2-review-the-parameters)
- [3. Deploy](#3-deploy)
- [Google sign-in](#google-sign-in)
- [4. Verify](#4-verify)
- [5. Automatic deploys from GitHub](#5-automatic-deploys-from-github)
- [Production](#production) (prod parameters, custom domain, promotion, approval)
- [Observability](#observability) (Application Insights, workbook)
- [Alerts and budgets](#alerts-and-budgets)
- [Backups and restore](#backups-and-restore)
- [Maintenance job](#maintenance-job) (abandoned uploads, data retention)
- [Rate and size limits](#rate-and-size-limits)
- [Configuration and secrets](#configuration-and-secrets)
- [Roll back](#roll-back)
- [Tear down and recreate](#tear-down-and-recreate)
- [Running locally against dev](#running-locally-against-dev)
- [Troubleshooting](#troubleshooting)

## What gets created

Resource group `jolt-dev` (region from the parameters file, default `centralindia`).
`<sfx>` is a stable hash of the subscription and resource group, so names are
globally unique and the same on every run.

| Resource | Name | Notes |
|---|---|---|
| User-assigned Managed Identity | `id-jolt-dev-api` | The app's only credential. |
| User-assigned Managed Identity | `id-jolt-dev-github` | GitHub Actions signs in as this (OIDC federated credential, no secret). |
| Log Analytics workspace | `log-jolt-dev` | Container Apps logs; 30-day retention, 1 GB/day cap. |
| Application Insights (workspace-based) | `appi-jolt-dev` | OpenTelemetry traces, logs and custom metrics from the app and the maintenance job (BE-10), stored in `log-jolt-dev`. |
| Azure Monitor workbook | "Jolt API (dev)" | Charts of the custom metrics ([Observability](#observability)). |
| Container Registry (Basic) | `crjoltdev<sfx>` | Images `jolt-api:<git-sha>`. |
| Cosmos DB for NoSQL, **serverless** | `cosmos-jolt-dev-<sfx>` | Database `jolt`; the 14 containers (`tokens` added by BE-03, `sync_runs` by BE-04, `review_sessions` by BE-06), partition keys and composite indexes of LLD §6 (from `infra/cosmos-containers.json`, kept equal to `jolt/data/cosmos.py` by `tests/test_infra.py`). Account keys disabled. Periodic backup every 24 h, kept 48 h. |
| Storage account (StorageV2, LRS, Hot) | `stjoltdev<sfx>` | Private container `sources`; Blob CORS for the app origins; blob and container soft delete 7 days; a lifecycle rule deletes data exports (`sources/exports/`, BE-08) a day after they are written; shared keys disabled (uploads use user-delegation SAS). |
| Key Vault (RBAC) | `kv-jolt-dev-<sfx8>` | Secrets `token-hash-pepper` and `dev-auth-secret` (dev sign-in, BE-03); per-user provider keys `apikey-*` written by the app. Soft delete 7 days, no purge protection (the app purges deleted API keys). Audit log (every secret get / set / delete / purge, with the caller) to `log-jolt-dev` through a diagnostic setting (BE-10 security review): `AzureDiagnostics \| where ResourceProvider == "MICROSOFT.KEYVAULT"`. |
| Container Apps environment | `cae-jolt-dev` | Consumption plan. |
| Container App | `jolt-api` | External HTTPS ingress → port 8000, `/health` startup/readiness/liveness probes, scale 0..1, 0.5 vCPU / 1 GiB. |
| Container Apps Job (scheduled) | `jolt-maintenance` | `python -m jolt.maintenance` daily at 03:17 UTC, same image and identity ([Maintenance job](#maintenance-job)). |
| Action group, alerts, budget | `ag-jolt-dev`, `jolt-dev-*` | Only when `alertEmails` is set (off in dev by default; on in prod). [Alerts and budgets](#alerts-and-budgets). |

Role assignments (least privilege):

| Who | Role | Scope | Why |
|---|---|---|---|
| `id-jolt-dev-api` | Cosmos DB Built-in Data Contributor (data plane) | database `jolt` | Read/write documents. Cannot create containers, so Bicep does. |
| `id-jolt-dev-api` | Storage Blob Data Contributor | storage account | Read/write blobs, check uploads, and `generateUserDelegationKey` for the SAS URLs (so Storage Blob Delegator is not needed on top). |
| `id-jolt-dev-api` | Key Vault Secrets Officer | vault | Read the pepper; set/delete/purge per-user API keys (Secrets User would be read-only). |
| `id-jolt-dev-api` | AcrPull | registry | Pull the image without a password. |
| `id-jolt-dev-github` | AcrPush + Reader | registry | Push images; `az acr login` needs to read the registry. |
| `id-jolt-dev-github` | Container Apps Contributor | resource group | `az containerapp update` (includes joining the environment). |
| `id-jolt-dev-github` | Managed Identity Operator | `id-jolt-dev-api` | Updating an app re-asserts its identity. |
| `id-jolt-dev-github` | Key Vault Secrets User | secret `dev-auth-secret` only | The deploy smoke test signs in with the dev route (only when `devAuthEnabled`). |
| you (optional) | Cosmos Data Contributor, Blob Data Contributor, Secrets Officer | as above | Only if `developerPrincipalId` is set, for local runs against dev. |

**Registry: ACR, not GHCR.** The Container App pulls with its Managed Identity
(AcrPull), so no registry password or GitHub PAT has to be stored in Azure, and
GitHub pushes through the same OIDC identity that deploys. That matches LLD §5.1
("Managed Identity everywhere, no key material in images or env"). The cost is
the Basic tier, about US$5 a month. GHCR would be free but a private package needs
a long-lived PAT stored as a Container App registry secret.

**Cosmos free tier** (mentioned in LLD §10) applies only to provisioned
throughput, not to serverless. The brief asks for serverless; with dev traffic it
costs cents.

## Owner checklist

1. Have an Azure subscription where you are **Owner** (or Contributor + User
   Access Administrator): the deployment creates role assignments.
2. Install the Azure CLI (2.61 or newer), `az login`, pick the subscription.
3. Register the resource providers (once per subscription).
4. Review `infra/main.dev.bicepparam`: region, Netlify origins, optional
   `developerPrincipalId`.
5. Create the Google OAuth client id and put it in `googleOAuthClientId`
   ([Google sign-in](#google-sign-in)). It can also be done after the first
   deploy (then rerun with `--skip-build`); until then Google sign-in answers 503.
6. Run `scripts/deploy-infra.sh`.
7. Check `https://<fqdn>/health` and run the smoke test.
8. Set seven GitHub repository variables (printed by the script, or let it set
   them with `--set-github-vars`).
9. In GitHub, restrict the `dev` environment to the `main` branch.
10. Optional for dev: alert e-mails and a budget (`alertEmails`, `budgetAmount`);
    prod has them ([Alerts and budgets](#alerts-and-budgets)).
11. Prod: [Production](#production) (its own checklist).

Nothing else is manual. The two secrets, the token-hash pepper and the dev
sign-in secret, are generated on the first deploy and never leave Key Vault (the
deploy workflow reads the dev sign-in secret from there, masked).

## 1. Prerequisites

```bash
az version                         # azure-cli >= 2.61 (Bicep params files + readEnvironmentVariable)
az login
az account set --subscription "<subscription name or id>"
az account show --query "{name:name, id:id, user:user.name}" -o table

for ns in Microsoft.App Microsoft.ContainerRegistry Microsoft.DocumentDB Microsoft.KeyVault \
          Microsoft.ManagedIdentity Microsoft.OperationalInsights Microsoft.Storage \
          Microsoft.Insights Microsoft.AlertsManagement Microsoft.Consumption; do
  az provider register --namespace "$ns"
done
```

Also needed on the machine that deploys: `git`, `openssl`, `python3` (stdlib only;
the smoke test needs `pip install -r requirements.txt`). Docker is needed only
with `--local-build`. `gh` only with `--set-github-vars`.

## 2. Review the parameters

`infra/main.dev.bicepparam` (lines marked `OWNER`):

| Parameter | Default | What to put |
|---|---|---|
| `location` | `centralindia` | Any region with Container Apps and Cosmos DB serverless. |
| `netlifyOrigins` | `https://jolt-app.netlify.app` | The real Netlify site origin(s) and any custom domain, exact, no trailing slash. |
| `corsOriginRegex` | deploy previews of `jolt-app` | Adjust the site name. API only (see "Netlify previews" below). |
| `developerPrincipalId` | empty | Your Entra object id (`az ad signed-in-user show --query id -o tsv`) if you want to run the server locally against dev. |
| `githubRepository` | `Dhiwakar1997/jolt-backend` | The repo allowed to deploy. Empty disables the GitHub identity. |
| `githubEnvironment` | `dev` | Must match `environment:` in `deploy-dev.yml`. |
| `maxReplicas` | `1` | Keep 1 while MCP uses SSE (see Troubleshooting). |
| `googleOAuthClientId` | empty | The Google OAuth client id ([Google sign-in](#google-sign-in)). Comma-separate several (web + mobile). |
| `mcpServerUrl` | empty | Leave empty for `https://<app FQDN>/mcp/sse`; set it when a custom domain fronts the API. Shown in the app by `GET /v1/me`. |
| `devAuthEnabled` | `true` | Dev sign-in by email (`POST /v1/auth/dev`) for the deploy smoke test, guarded by `dev-auth-secret`. Must be `false` in any prod parameters file: the app refuses to start with it in prod. |

**Netlify previews.** API CORS accepts the regex, so every deploy preview can call
`/v1`. Azure Blob CORS only takes exact origins (or `*`), so a preview can upload
directly to Blob only if its origin is listed in `netlifyOrigins`. Options: test
uploads on the production/branch-deploy URL, add a fixed branch-deploy origin
(`https://<branch>--jolt-app.netlify.app`), or, for dev only, set
`netlifyOrigins = ['*']` (the SAS URL is still the authorization; `*` then also
lands in `CORS_ORIGINS`).

## 3. Deploy

From the repository root, on the commit you want to run:

```bash
scripts/deploy-infra.sh
```

What it does:

1. Reads the resource group and region from `infra/main.dev.bicepparam`.
2. If Key Vault has no `token-hash-pepper` or `dev-auth-secret` secret yet
   (checked through ARM, no data-plane access needed), generates it with
   `openssl rand` and passes it to the deployment through an environment
   variable. Later runs never touch them.
3. On a fresh environment: deploys everything except the Container App (the
   registry has to exist before an image can be pushed).
4. Builds the image from the current commit in Azure with `az acr build`
   (tag = git SHA, `-dirty-<time>` appended if the tree has local changes).
5. Deploys everything with that image (`az deployment sub create`, subscription
   scope, so the resource group is created too).
6. Waits for `https://<fqdn>/health` and prints the URLs and the GitHub variables.

Useful flags: `--smoke` (run the smoke test afterwards), `--set-github-vars`,
`--skip-build` (infra/config change only, keep the running image),
`--image <ref>` (deploy a given image), `--local-build` (Docker on your machine
instead of ACR Tasks), `--what-if` (preview, change nothing). The script is
idempotent: run it again after any change to `infra/`.

The equivalent raw command, if you prefer to drive it yourself:

```bash
export JOLT_IMAGE=<acr>.azurecr.io/jolt-api:<sha>   # empty: everything but the app
export JOLT_TOKEN_HASH_PEPPER="$(openssl rand -base64 48)"   # first deploy only, else ""
export JOLT_DEV_AUTH_SECRET_INIT="$(openssl rand -hex 32)"    # first deploy only, else ""
az deployment sub create -n jolt-dev -l centralindia \
  -f infra/main.bicep -p infra/main.dev.bicepparam
```

## Google sign-in

The app signs people in with Google (decision D4): the browser gets an ID token
from Google Identity Services and posts it to `POST /v1/auth/google`, which
verifies it against Google's public keys (fetched from
`https://www.googleapis.com/oauth2/v3/certs` and cached, so the app needs
outbound HTTPS to Google, which Container Apps has by default) and checks that
it was issued to Jolt's client id. No client secret is involved.

1. Google Cloud console → APIs & Services → OAuth consent screen: configure it
   (External, app name "Jolt", support e-mail; scopes `openid`, `email`,
   `profile`). While it is in "Testing", add the testers' Google accounts.
2. APIs & Services → Credentials → Create credentials → OAuth client ID →
   type **Web application**. Authorized JavaScript origins: every origin the app
   runs on, exactly as in `appOrigins` / `netlifyOrigins` (e.g.
   `http://localhost:5173`, `https://jolt-app.netlify.app`). No redirect URI is
   needed for the ID-token flow. Netlify deploy previews cannot be listed by
   pattern; use the production or a fixed branch-deploy origin to test sign-in.
3. Copy the client id (`<number>-<hash>.apps.googleusercontent.com`) into
   `googleOAuthClientId` in `infra/main.dev.bicepparam` and run
   `scripts/deploy-infra.sh --skip-build`. The front end needs the same client
   id in its own configuration.

Check: `POST /v1/auth/google` with a garbage token now answers 401 (it answered
503 "not configured" before).

## 4. Verify

```bash
FQDN=$(az containerapp show -g jolt-dev -n jolt-api --query properties.configuration.ingress.fqdn -o tsv)
curl -s https://$FQDN/health            # {"status":"ok","version":"<git sha>"}
pip install -r requirements.txt
python scripts/smoke.py --base-url https://$FQDN
```

The smoke test signs in a throwaway user (`smoke-<timestamp>-<random>@smoke.jolt.test`)
with the dev route and walks the whole loop: health, `GET /v1/me`, create an MCP
token (and check `/mcp` refuses the app token), MCP handshake and tool list,
upload URL → PUT a PNG to Blob → confirm, the sync tool sequence with canned
extraction and questions, due reviews, stage 1 and stage 2, agent grading,
coverage, a final `jolt_sync` that closes the sync run, `GET /v1/sync/status` /
`/v1/sync/runs` / `/v1/sync/prompt`, an MCP connection with the token in the URL
(`?token=`), progress, the settings round trip and an export, then revokes the
MCP token, signs one device out and deletes the throwaway user with
`DELETE /v1/me` (each refused afterwards). When the curated DSA track is
imported it also enrols, reads the library and decision tree, answers one
`pattern_match` question (BE-09) and checks the DSA track is in progress;
without it those steps are skipped with a note (`--require-dsa` makes that a
failure). Exit code 0 means pass. It needs the dev sign-in secret in
`JOLT_DEV_AUTH_SECRET` (or `--dev-auth-secret`); `deploy-infra.sh --smoke` and
the deploy workflow supply it. By hand, with Key Vault data access:
`export JOLT_DEV_AUTH_SECRET=$(az keyvault secret show --vault-name <kv> -n dev-auth-secret --query value -o tsv)`.
The first request after idle takes a few seconds (scale from zero); the script
waits up to `--wait` seconds (default 120) for `/health`.

**Curated DSA track (BE-09, deploy pending).** Import it once per environment
(dev and prod), and again whenever Jolt's `data/` or DSA content changes (a
re-run with the same input writes nothing), from a machine with Cosmos data
access (the same `COSMOS_ENDPOINT` / `AUTH_MODE` settings as the app;
`AUTH_MODE=managed_identity` uses your `az login`). `DSA_SEED_FROM` is for
memory mode only; against Cosmos the import is always this script. Account
deletes and the maintenance sweep never touch the curated content.

```bash
python scripts/import_dsa.py --from ../Jolt --dry-run
python scripts/import_dsa.py --from ../Jolt
```

MCP clients connect to `https://<fqdn>/mcp/sse` (the `mcpServerUrl` output, also
shown in the app) with header `Authorization: Bearer <MCP token>`, or with
`?token=<MCP token>` on that URL for Claude's URL-only custom connectors. A person
creates MCP tokens in the app (`POST /v1/mcp-tokens`); the app's own token is
refused on `/mcp`. Exact Claude Desktop and Codex setup, and what to confirm with
the real clients after a deploy: [`CONNECT.md`](./CONNECT.md).

## 5. Automatic deploys from GitHub

`.github/workflows/deploy-dev.yml` runs on every push to `main` (and on manual
dispatch): the CI checks, then build → push to ACR → `az containerapp update` →
read `dev-auth-secret` from Key Vault (masked) → `scripts/smoke.py --expect-version <sha>`
(waits until the new revision answers).

Set these **repository variables** (Settings → Secrets and variables → Actions →
Variables; not secrets, none of them is sensitive). `deploy-infra.sh` prints
them; `--set-github-vars` sets them with `gh`:

| Variable | Value |
|---|---|
| `AZURE_CLIENT_ID` | client id of `id-jolt-dev-github` (output `githubDeployClientId`) |
| `AZURE_TENANT_ID` | your Entra tenant id |
| `AZURE_SUBSCRIPTION_ID` | the subscription id |
| `AZURE_RESOURCE_GROUP` | `jolt-dev` |
| `ACR_NAME` | `crjoltdev<sfx>` (output `registryName`) |
| `CONTAINER_APP_NAME` | `jolt-api` |
| `KEYVAULT_NAME` | `kv-jolt-dev-<sfx8>` (output `keyVaultName`); the smoke test reads `dev-auth-secret` from it |

They must be repository variables, not `dev` environment variables: the job's
`if:` reads `vars.AZURE_CLIENT_ID` before the environment is entered. Until it is
set, the deploy job is skipped (never failed), so CI stays green.

Sign-in is OIDC: the federated credential on `id-jolt-dev-github` trusts tokens
with subject `repo:Dhiwakar1997/jolt-backend:environment:dev` only. In GitHub,
open Settings → Environments → `dev` (created by the first run, or create it) and
add a deployment branch rule for `main`, so no other branch can use that subject.

To check: push to `main` (or Actions → Deploy dev → Run workflow on `main`) and
watch the job; it ends with `SMOKE PASSED`.

## Production

`prod` is the same template with `infra/main.prod.bicepparam`, in resource group
`jolt-prod`. Values marked `OWNER` there are placeholders (`example.com`) the
owner replaces with the real domain, origins and recipients before the first
deploy. What differs from dev, and why:

| Setting | dev | prod | Why |
|---|---|---|---|
| Dev sign-in (`devAuthEnabled`) | on (smoke test) | **off** | The app refuses to start with it in prod; no `dev-auth-secret` is generated. |
| CORS origins | localhost + Netlify + previews regex | **the production origin(s) only** | No localhost, no deploy previews (`corsOriginRegex = ''`). |
| `minReplicas` / `maxReplicas` | 0 / 1 | **1 / 1** | One warm replica: no cold start for the first request of the day or a nightly sync. About US$10–15 a month at the idle rate after the free grant. Max stays 1 while MCP uses SSE, which also makes the per-replica rate limits exact. |
| Custom domain | none | **`customDomain`** + managed certificate | [Custom domain](#custom-domain). |
| Cosmos backup | periodic (24 h, kept 48 h) | **continuous, 30 days** | Self-service point-in-time restore to any second of the last 30 days; cents at this size. One-way switch. |
| Blob | soft delete 7 days | **soft delete 14 days + versioning** | Overwritten or deleted blobs keep a version, removed by a lifecycle rule after 14 days. |
| Key Vault soft delete | 7 days | **90 days** | A deleted pepper stays recoverable. Fixed when the vault is created. Purge protection stays off in both (the app purges per-user API keys on delete). |
| Logs | 30 days, 1 GB/day | **90 days, 5 GB/day** | |
| Alerts, budgets | off | **on** (`alertEmails`, `budgetAmount`, `subscriptionBudgetAmount`) | [Alerts and budgets](#alerts-and-budgets). |
| Images | built from `main` | **promoted from dev** | Never rebuilt: the digest that passed the dev smoke test is what runs. |
| GitHub identity | `id-jolt-dev-github`, environment `dev` | `id-jolt-prod-github`, environment **`prod`** | Also gets AcrPull + Reader on the dev registry to copy images (`promoteFromEnvironment = 'dev'`). |

### Prod checklist

1. Edit `infra/main.prod.bicepparam`: `appOrigins` (the production front end),
   `customDomain`, `googleOAuthClientId` (add the production origin to the
   OAuth client's Authorized JavaScript origins), `alertEmails`, the budget
   amounts and `budgetStartDate` (first day of a month, not in the past).
2. Dev must be deployed (the promotion source) and the image to promote must be
   in the dev registry: any `main` commit that `deploy-dev.yml` deployed.
3. First deploy (needs Owner, as for dev; it also writes a role assignment into
   `jolt-dev`):
   ```bash
   scripts/deploy-infra.sh --env prod --promote <git-sha> --set-github-vars
   ```
   The script refuses to build an image for prod. `--promote` deploys the
   infrastructure first if needed, copies `jolt-api:<sha>` from the dev
   registry with `az acr import` (same digest), then deploys the app.
4. Create the DNS records it prints ([Custom domain](#custom-domain)) and rerun
   `scripts/deploy-infra.sh --env prod --skip-build` until it reports the
   domain bound, then once more with `--smoke`.
5. GitHub: create the `prod` environment with **required reviewers**
   ([Promotion](#promotion-and-approval)).
6. Optional: set `PROD_APP_ORIGIN` (the production front-end origin) so the
   workflow's smoke test checks the CORS preflight.
7. Test the alert e-mails ([Alerts and budgets](#alerts-and-budgets)).

### Custom domain

The API is served on the custom domain (`var customDomain` in
`infra/main.prod.bicepparam`, a subdomain such as `api.jolt.example.com`) with
a free, auto-renewed managed certificate. Azure checks the DNS records when a
hostname is added to an app, a managed certificate can only be issued for a
hostname already on the app, and the app can only bind a certificate that
exists, so it takes three steps; `deploy-infra.sh` drives them and each rerun
moves on as far as DNS allows:

1. **Without the domain.** While the domain does not resolve, the script
   deploys without it (`JOLT_SKIP_CUSTOM_DOMAIN=true`) and prints the two DNS
   records to create at the domain's DNS host:

   | Type | Name | Value |
   |---|---|---|
   | CNAME | `api.jolt.example.com` | the app's default host (output `containerAppFqdn`, `jolt-api.<env-domain>.azurecontainerapps.io`) |
   | TXT | `asuid.api.jolt.example.com` | output `customDomainVerificationId` |

2. **Add it.** Once the CNAME resolves, `scripts/deploy-infra.sh --env prod --skip-build`
   adds the hostname to the app unbound (`bindingType: Disabled`) and requests
   the certificate (`infra/modules/certificate.bicep`, CNAME validation). If
   Azure issues it during the run, the script binds it straight away.
3. **Bind it.** Otherwise the next `--skip-build` run finds the certificate
   (`az containerapp env certificate list --managed-certificates-only`), sets
   `JOLT_CUSTOM_DOMAIN_CERT_READY=true` (read by `customDomainCertificateReady`)
   and binds it with SNI. From then on every deploy keeps it bound, and
   `MCP_SERVER_URL` and the output `publicUrl` become `https://<custom domain>`.

The default `*.azurecontainerapps.io` host keeps working. An apex domain
(`jolt.example.com` itself) needs an A record and HTTP validation instead;
use a subdomain. Check: `curl -sS https://api.jolt.example.com/health`.

### Promotion and approval

`.github/workflows/deploy-prod.yml` deploys an image that already exists, never
builds one:

- **Tag**: `git tag v2.0.0 <sha> && git push origin v2.0.0` promotes the image
  of the tagged commit (`jolt-api:<commit sha>`, which `deploy-dev.yml` built
  and smoke-tested when that commit reached `main`).
- **Manual**: Actions → Deploy prod → Run workflow, optionally `image_tag` = a
  SHA already in the dev registry (also how prod rolls back).

The job waits for an approval in the GitHub environment `prod`, then copies
`jolt-api:<sha>` from the dev registry to the prod registry (`docker pull` /
`push`, same digest), updates `jolt-api` and the `jolt-maintenance` job, and
runs `scripts/smoke.py --public --expect-dev-auth-off` against
`PROD_PUBLIC_URL` (health and version, 401 without or with a bogus token on
`/v1` and `/mcp`, Google sign-in configured, 413 for an oversized body, dev
sign-in 404, and the CORS preflight from `PROD_APP_ORIGIN` when set). Dev
sign-in is off in prod, so the smoke test creates no account.

Set up the gate once, in GitHub → Settings → Environments → **New environment**
`prod`:

1. **Required reviewers**: the owner (and anyone else allowed to ship). Tick
   "Prevent self-review" if there is more than one reviewer.
2. **Deployment branches and tags** → Selected branches and tags: add the tag
   pattern `v*` and the branch `main` (for manual runs).
3. Leave environment secrets empty: sign-in is OIDC, and the variables are
   repository variables (the job's `if:` reads them before the environment).

Repository variables (Settings → Secrets and variables → Actions → Variables;
`scripts/deploy-infra.sh --env prod --set-github-vars` sets them):

| Variable | Value |
|---|---|
| `PROD_AZURE_CLIENT_ID` | client id of `id-jolt-prod-github` |
| `PROD_AZURE_RESOURCE_GROUP` | `jolt-prod` |
| `PROD_ACR_NAME` | `crjoltprod<sfx>` |
| `PROD_CONTAINER_APP_NAME` | `jolt-api` |
| `PROD_KEYVAULT_NAME` | `kv-jolt-prod-<sfx8>` (not used by the workflow; for reference) |
| `PROD_PUBLIC_URL` | `https://<custom domain>` (output `publicUrl`) |
| `PROD_APP_ORIGIN` | optional: the production front-end origin |
| `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `ACR_NAME` | shared with dev (`ACR_NAME` is the dev registry, the source) |

Until `PROD_AZURE_CLIENT_ID` is set the job is skipped. The federated credential
on `id-jolt-prod-github` trusts only `repo:Dhiwakar1997/jolt-backend:environment:prod`,
so only a job that passed the approval can sign in as it.

**Roll back prod**: Actions → Deploy prod → Run workflow → `image_tag` = the
previous SHA (it is still in both registries), or
`az containerapp update -g jolt-prod -n jolt-api --image <prod acr>.azurecr.io/jolt-api:<sha>`.

## Observability

The app exports traces, logs and metrics with OpenTelemetry (the Azure Monitor
distro, `jolt/telemetry.py`) to the workspace-based Application Insights
`appi-jolt-<env>`, only when `APPLICATIONINSIGHTS_CONNECTION_STRING` is set
(Bicep sets it as a Container Apps secret; tests and local runs leave it empty
and load nothing):

- **Requests** (`requests` table): every HTTP request except `/health`, with
  duration and result code; **dependencies**: Cosmos, Blob, Key Vault and
  Google calls; **traces**: the `jolt.*` loggers (never the access log).
- **Custom metrics** (`customMetrics`, attributes in `customDimensions`):

  | Metric | What | Attributes |
  |---|---|---|
  | `jolt.sync.runs` | sync runs started / finished / closed incomplete | `user_id` (opaque id, never an e-mail), `status`, `client` |
  | `jolt.extraction.confidence` | confidence of every stored read (histogram) | `low_confidence`, `model` |
  | `jolt.lease.expiries` | sync leases that lapsed: reclaimed by a later sync, or the source marked failed | `kind`, `outcome` |
  | `jolt.cosmos.request_charge` | Cosmos RU per REST request and per MCP tool call (histogram), from the `x-ms-request-charge` header of every Cosmos response | `surface` (`rest` / `mcp`), `operation` (`GET /v1/sources/{source_id}` / tool name) |
  | `jolt.http.requests` | requests by route template and status | `route`, `method`, `status_code`, `status_class` |
  | `jolt.mcp.tool_calls` | tool calls by outcome | `tool`, `outcome` (`ok` / `error` / `unauthorized` / `rate_limited`) |
  | `jolt.rate_limited` | 429s | `surface`, `scope` (`token` / `ip`) |
  | `jolt.maintenance.deleted` | what the maintenance job removed | `kind` |

The **workbook** "Jolt API (<env>)" (Azure portal → Monitor → Workbooks, or the
Application Insights resource → Workbooks; source `infra/workbook.json`) charts
sync runs per day and per user, the extraction-confidence distribution and
mean, lease expiries and the expiry rate per stored read, RU per tool and per
route, HTTP 4xx/5xx rates, MCP tool outcomes, failing routes, 429s, exceptions
and maintenance deletions. To change it, edit it in the portal, copy the
gallery template JSON (Advanced editor) into `infra/workbook.json`, redeploy.

Ad hoc: Application Insights → Logs, e.g.
`customMetrics | where name == "jolt.cosmos.request_charge" | summarize sum(valueSum) by tostring(customDimensions.operation)`.

## Alerts and budgets

With `alertEmails` set (prod; optional in dev), `infra/modules/alerts.bicep`
creates the action group `ag-jolt-<env>` (e-mail, common alert schema) and:

| Rule | Fires when | Parameter (prod default) |
|---|---|---|
| `jolt-<env>-cosmos-ru` | Cosmos `TotalRequestUnits` over the last hour above the threshold (evaluated every 15 min) | `cosmosRuAlertPerHour` (1,000,000 RU ≈ US$0.25 serverless) |
| `jolt-<env>-blob-capacity` | `BlobCapacity` (average over 6 h) above the threshold | `blobCapacityAlertGb` (50 GiB) |
| `jolt-<env>-http-5xx` | Container Apps `Requests` with status 5xx in 5 minutes above the count | `http5xxAlertCount` (10) |
| `jolt-<env>-http-5xx-rate` | Application Insights: failed (≥ 500) share of requests over 15 min above the percentage, with at least 20 requests | `http5xxAlertRatePercent` (5) |
| Budget `jolt-<env>-monthly` | resource-group cost: actual at 80 %, forecast at 100 % | `budgetAmount` (50), `budgetStartDate` |
| Budget `jolt-subscription-monthly` | whole subscription, same thresholds (set in prod only) | `subscriptionBudgetAmount` (100) |

Amounts are in the subscription's billing currency. Budgets e-mail
`alertEmails` and notify the action group.

**Test that alerts reach people** (after a deploy; "deploy pending" until then):

```bash
# Sends a sample notification of each kind through the action group:
az monitor action-group test-notifications create -g jolt-prod \
  --action-group ag-jolt-prod --alert-type budget
az monitor action-group test-notifications create -g jolt-prod \
  --action-group ag-jolt-prod --alert-type metricstaticthreshold
```

To fire a real rule end to end, deploy once with `blobCapacityAlertGb = 0`
(any stored blob crosses it; fires within about an hour), confirm the e-mail,
then set it back and redeploy.

## Backups and restore

| Store | dev | prod | Restore |
|---|---|---|---|
| Cosmos DB | periodic: every 24 h, kept 48 h | **continuous**, 30-day point in time | below |
| Blob (`sources`) | soft delete 7 days (blobs and containers) | soft delete 14 days + **versioning** (versions kept 14 days) | below |
| Key Vault | soft delete 7 days | soft delete 90 days | `az keyvault secret recover --vault-name <kv> -n token-hash-pepper` |

**Cosmos, prod (continuous).** A point-in-time restore creates a **new**
account; the live one is never overwritten.

```bash
az cosmosdb restorable-database-account list --account-name cosmos-jolt-prod-<sfx> -o table
az cosmosdb restore -g jolt-prod \
  --account-name cosmos-jolt-prod-<sfx> \
  --target-database-account-name cosmos-jolt-prod-restore-<date> \
  --restore-timestamp 2026-12-01T10:00:00Z --location centralindia
```

Then either copy the needed documents back (small incidents: one user's data),
or switch the app to the restored account: give `id-jolt-prod-api` the Cosmos
DB Built-in Data Contributor role on it (as `infra/modules/cosmos.bicep` does),
`az containerapp update -g jolt-prod -n jolt-api --set-env-vars COSMOS_ENDPOINT=<restored endpoint>`
(and the same on the `jolt-maintenance` job), and later fold the change into
Bicep. A deleted database or container can also be restored in place:
`az cosmosdb sql container restore -g jolt-prod -a cosmos-jolt-prod-<sfx> -d jolt -n <container> --restore-timestamp <time>`.
**Account deletes:** a restore brings back users who deleted their account
after the restore point; delete them again (their ids are in the app logs:
`account deleted: user=<id>`) before switching over.

**Cosmos, dev (periodic):** no self-service restore; open a support request
in the portal (Help + support → Create a support request → Azure Cosmos DB,
problem type "Backup and restore") within 48 h of the incident.

**Blob.** Soft-deleted blobs are listed and undeleted with
`az storage blob list --account-name <st> -c sources --include d --prefix <user_id>/ --auth-mode login`
and `az storage blob undelete --account-name <st> -c sources -n <path> --auth-mode login`.
In prod, versioning keeps the previous version of a deleted or overwritten
blob; restore one by copying it over the current name:
`az storage blob list ... --include v`, then
`az storage blob copy start --account-name <st> --destination-container sources --destination-blob <path> --source-uri "https://<st>.blob.core.windows.net/sources/<path>?versionid=<id>" --auth-mode login`.
A deleted container: `az storage container list --include-deleted` and
`az storage container restore -n sources --deleted-version <v>`. The deploying
user needs a Blob data role for these (e.g. via `developerPrincipalId` in dev,
or a temporary Storage Blob Data Contributor assignment in prod).

Rehearse a Cosmos restore and a blob undelete once after the first prod
deploy, and after any change to `infra/modules/cosmos.bicep` or
`storage.bicep`.

## Maintenance job

`jolt-maintenance` is a scheduled Container Apps Job (`maintenanceSchedule`,
default `17 3 * * *` UTC) running `python -m jolt.maintenance` from the app's
image, with the app's identity and store settings (no ingress, no pepper). Each
run:

1. deletes **abandoned uploads**: sources whose upload was requested but never
   confirmed for `ABANDONED_UPLOAD_HOURS` (24), with any pages that arrived;
2. enforces **data retention**: for each user with `data_retention_days`, deletes
   sources uploaded longer ago than that, with their blobs and reads
   (extractions). Concepts, questions and review history stay.

It prints a JSON report and logs it (and the `jolt.maintenance.deleted`
metric); a failure exits 1, which shows as a failed execution. The deploy
workflows roll the job to each new image. By hand:

```bash
az containerapp job start -g jolt-prod -n jolt-maintenance          # run now
az containerapp job execution list -g jolt-prod -n jolt-maintenance -o table
az containerapp job logs show -g jolt-prod -n jolt-maintenance --container jolt-maintenance
python -m jolt.maintenance --dry-run                                # locally, against .env
```

There is deliberately no HTTP route for it. Set `maintenanceEnabled = false` to
remove nothing on a schedule (the job is then not created; delete an existing
one with `az containerapp job delete`).

## Rate and size limits

In-process token buckets, **per replica** (`jolt/api/limits.py`):
`RATE_LIMIT_REST_PER_MINUTE` (120) per app token on `/v1` (sign-in and other
token-less requests per client IP), `RATE_LIMIT_MCP_PER_MINUTE` (300) per MCP
token for tool calls and connections, and `RATE_LIMIT_IP_PER_MINUTE` (600) per
IP across both. Over a limit: `429` with `Retry-After`. Bodies over
`REQUEST_MAX_BYTES` (1 MiB; `MCP_REQUEST_MAX_BYTES` 4 MiB on `/mcp`) get `413`.
The client IP is the right-most `X-Forwarded-For` entry, the one Container Apps
ingress appends (`TRUSTED_PROXY_HOPS=1`, set by Bicep): a client that sends its
own `X-Forwarded-For` cannot pick a fresh bucket per request (BE-10 security
review). With more than one replica each enforces its own buckets;
a shared limiter (Redis) is only worth it once `maxReplicas` > 1. Change a
limit without a code change: `az containerapp update -g <rg> -n jolt-api --set-env-vars RATE_LIMIT_REST_PER_MINUTE=240`
(or add it to `infra/modules/containerapp.bicep`).

## Configuration and secrets

Every variable is documented in `.env.example`. In Azure they are set on the
container by Bicep (`infra/modules/containerapp.bicep`):

| Variable | Value in dev | Source | Owner action |
|---|---|---|---|
| `JOLT_ENV` | `dev` | Bicep | — |
| `STORAGE_MODE` | `azure` | Bicep | — |
| `AUTH_MODE` | `managed_identity` | Bicep | — |
| `AZURE_CLIENT_ID` | client id of `id-jolt-dev-api` | Bicep | — |
| `COSMOS_ENDPOINT`, `COSMOS_DATABASE` | account endpoint, `jolt` | Bicep | — |
| `BLOB_ACCOUNT_URL`, `BLOB_CONTAINER` | blob endpoint, `sources` | Bicep | — |
| `KEYVAULT_URL` | vault URI | Bicep | — |
| `CORS_ORIGINS` | `appOrigins` + `netlifyOrigins` | Bicep param | set Netlify origins |
| `CORS_ORIGIN_REGEX` | `corsOriginRegex` | Bicep param | adjust site name |
| `TOKEN_HASH_PEPPER` | Key Vault secret `token-hash-pepper` (secret reference via the app identity) | generated once by `deploy-infra.sh` | none; never rotate casually (it invalidates every token) |
| `GOOGLE_OAUTH_CLIENT_ID` | `googleOAuthClientId` | Bicep param | create the OAuth client ([Google sign-in](#google-sign-in)) |
| `MCP_SERVER_URL` | `https://<app FQDN>/mcp/sse`, or `mcpServerUrl` | Bicep | — (set `mcpServerUrl` for a custom domain) |
| `DEV_AUTH_ENABLED` | `true` (from `devAuthEnabled`) | Bicep param | `false` in prod |
| `DEV_AUTH_SECRET` | Key Vault secret `dev-auth-secret` (secret reference), only when `devAuthEnabled` | generated once by `deploy-infra.sh` | none |
| `APP_VERSION` | git SHA | Docker build arg | — |
| `COSMOS_KEY`, `BLOB_*_KEY`, `BLOB_CONNECTION_STRING` | unset | — | — (keys are disabled) |
| `BLOB_SAS_TTL_SECONDS`, `LEASE_TTL_SECONDS`, `FSRS_DESIRED_RETENTION`, `APP_TOKEN_PREFIX`, `MCP_TOKEN_PREFIX`, `*_API_BASE` | defaults | code | — |
| `SYNC_RUN_IDLE_SECONDS` (1800), `SYNC_LATE_AFTER_HOURS` (30), `SYNC_SCHEDULE_TIME` (`00:15`), `MCP_QUERY_TOKEN_ENABLED` (`true`) | defaults | code (BE-04) | — |
| `EXPORT_URL_TTL_SECONDS` (3600) | default | code (BE-08) | — |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | `appi-jolt-<env>`'s connection string (Container Apps secret `appinsights-connection-string`) | Bicep | — |
| `RATE_LIMIT_ENABLED` (`true`), `RATE_LIMIT_REST_PER_MINUTE` (120), `RATE_LIMIT_MCP_PER_MINUTE` (300), `RATE_LIMIT_IP_PER_MINUTE` (600), `REQUEST_MAX_BYTES` (1 MiB), `MCP_REQUEST_MAX_BYTES` (4 MiB) | defaults | code (BE-10) | — ([Rate and size limits](#rate-and-size-limits)) |
| `ABANDONED_UPLOAD_HOURS` (24) | default | code (BE-10), read by the maintenance job | — |
| `TRUSTED_PROXY_HOPS` | `1` (the ingress) | Bicep (BE-10 security review) | — |
| `APP_TOKEN_IDLE_DAYS` (30) | default | code (BE-10 security review): app tokens unused this long are refused and swept | — |
| `API_DOCS_ENABLED` | unset: `/docs`, `/redoc`, `/openapi.json` on in dev, off in prod | code (BE-10 security review) | — |
| `MODEL_DAILY_REQUEST_CAP` (50), `MODEL_DAILY_TOKEN_CAP` (2,000,000) | `modelDailyRequestCap`, `modelDailyTokenCap` | Bicep param (BE-11) | — (per-user daily spend guard on users' own keys; 0 turns feedback and read now off) |
| `ANTHROPIC_MODEL`, `OPENAI_MODEL` | `anthropicModel`, `openaiModel` | Bicep param (BE-11) | **OWNER**: a current model id from each provider's model list. No default: while empty, that provider's feedback and read now answer `provider_error` (503) without a request. |
| `ANTHROPIC_SEND_EFFORT`, `OPENAI_SEND_REASONING`, `ANTHROPIC_REFUSAL_FALLBACK`, `MODEL_*_TIMEOUT_SECONDS`, `MODEL_*_MAX_TOKENS`, `READ_NOW_MAX_*_BYTES` | defaults | code (BE-11) | — ([`API.md`](./API.md) section 8) |

With `STORAGE_MODE=azure` the API refuses to start if `TOKEN_HASH_PEPPER` is the
`.env.example` placeholder or shorter than 32 characters. Security findings,
what was fixed and what is accepted: [`SECURITY.md`](./SECURITY.md).

To change a value without a code change, edit the Bicep parameter and run
`scripts/deploy-infra.sh --skip-build`. To rotate the pepper (logs every user
out): `az keyvault secret set --vault-name <kv> -n token-hash-pepper --value "$(openssl rand -base64 48)"`
then restart the app (`az containerapp revision restart`); Container Apps also
re-reads Key Vault references periodically.

**Budgets** are Bicep parameters now (`budgetAmount`, `subscriptionBudgetAmount`,
`alertEmails`): [Alerts and budgets](#alerts-and-budgets).

## Roll back

The app runs in single-revision mode; every deploy is an immutable image
`jolt-api:<git-sha>` in ACR, so rolling back means pointing the app at an older tag.

- **From GitHub:** Actions → Deploy dev → Run workflow (branch `main`) →
  `image_tag` = the SHA to go back to. It skips the build, checks the tag exists,
  deploys it and runs the smoke test against it.
- **From a shell:**
  ```bash
  az acr repository show-tags -n <acr> --repository jolt-api --orderby time_desc -o table
  az containerapp update -g jolt-dev -n jolt-api --image <acr>.azurecr.io/jolt-api:<sha>
  # or: scripts/deploy-infra.sh --image <acr>.azurecr.io/jolt-api:<sha>
  az containerapp revision list -g jolt-dev -n jolt-api -o table
  ```
- **Infrastructure:** check out the previous commit of `infra/` and run
  `scripts/deploy-infra.sh --skip-build`. Deployments are incremental: resources
  removed from Bicep are not deleted, so remove them by hand if needed.
- **Data:** [Backups and restore](#backups-and-restore). Deleted Key Vault
  secrets are recoverable for the soft-delete window unless the app purged them
  (API-key deletes purge on purpose).

## Tear down and recreate

```bash
az group delete -n jolt-dev --yes
# Key Vault names stay reserved while soft-deleted; purge to reuse the name:
az keyvault list-deleted --query "[?starts_with(name, 'kv-jolt-dev-')].name" -o tsv \
  | xargs -r -n1 az keyvault purge --name
```

Then `scripts/deploy-infra.sh` builds a clean environment again (a new pepper and
dev sign-in secret are generated, so old tokens die with the old data; update
nothing in GitHub except `KEYVAULT_NAME` if the vault name changed).

## Running locally against dev

1. Set `developerPrincipalId` in the params file and redeploy (`--skip-build`).
2. `.env`:
   ```
   STORAGE_MODE=azure
   AUTH_MODE=managed_identity
   COSMOS_ENDPOINT=<output cosmosEndpoint>
   BLOB_ACCOUNT_URL=<output blobAccountUrl>
   KEYVAULT_URL=<output keyVaultUri>
   TOKEN_HASH_PEPPER=<az keyvault secret show --vault-name <kv> -n token-hash-pepper --query value -o tsv>
   GOOGLE_OAUTH_CLIENT_ID=<the client id, if you sign in with Google locally>
   ```
3. `az login`, then `uvicorn jolt.main:app --reload`. DefaultAzureCredential uses
   your CLI login.

Without Azure at all: `STORAGE_MODE=memory DEV_AUTH_ENABLED=true PUBLIC_BASE_URL=http://localhost:8000 uvicorn jolt.main:app --port 8000`
(uploads are served by `/_dev/blob`; sign in with `POST /v1/auth/dev {"email": "you@example.com"}`,
no secret needed in memory mode), and `python scripts/smoke.py --base-url http://localhost:8000`.

## Troubleshooting

- **Logs:** `az containerapp logs show -g jolt-dev -n jolt-api --follow`
  (add `--type system` for pulls, probes, Key Vault references), or query
  `ContainerAppConsoleLogs_CL` in `log-jolt-dev`.
- **`az acr build` fails with "TasksOperationsNotAllowed"** (some new or trial
  subscriptions): `scripts/deploy-infra.sh --local-build`.
- **First revision fails to pull the image or resolve the Key Vault secret:**
  new role assignments can take a few minutes to propagate. Run
  `scripts/deploy-infra.sh --skip-build` again.
- **"The vault name is already in use" after a teardown:** purge the soft-deleted
  vault (see above).
- **MCP "Could not find session" errors:** the SSE stream and its POSTs reached
  different replicas. MCP SSE sessions live in process memory, which is why
  `maxReplicas` is 1. Raising it needs a stateless transport (streamable HTTP) or
  session affinity first.
- **Browser upload blocked by CORS:** the page's origin must be in
  `netlifyOrigins`/`appOrigins` exactly (scheme + host + port, no slash);
  redeploy with `--skip-build`.
- **Google sign-in answers 503 "not configured":** `googleOAuthClientId` is empty;
  see [Google sign-in](#google-sign-in). **401 "issued for another client":** the
  front end uses a different client id than `googleOAuthClientId`.
- **Smoke test fails at `POST /v1/auth/dev` with 403:** wrong or missing
  `JOLT_DEV_AUTH_SECRET`; with 404: `devAuthEnabled` is false. In the workflow,
  check the `KEYVAULT_NAME` variable and that the deploy ran Bicep once since
  BE-03 (it grants the GitHub identity read access to that one secret).
- **Deploy job skipped in GitHub:** `AZURE_CLIENT_ID` repository variable missing,
  or the run is not on `main`.
- **429 Too Many Requests:** a client went over its per-token budget
  ([Rate and size limits](#rate-and-size-limits)); the `jolt.rate_limited`
  metric and the workbook show which surface. Raise the limit with an env var
  if it is legitimate traffic.
- **No data in Application Insights:** check the app has
  `APPLICATIONINSIGHTS_CONNECTION_STRING` (`az containerapp show ... --query
  "properties.template.containers[0].env"`) and its log line
  `telemetry: exporting to Application Insights`; metrics arrive about a minute
  after the traffic.
- **Deploy fails adding the custom domain ("TXT record ... asuid" / "CNAME
  record ... not found"):** the TXT record is missing or not propagated; the
  CNAME already resolved, which is why the script tried. Fix DNS and rerun.
- **Custom domain answers with a certificate error:** the certificate is not
  bound yet. `az containerapp env certificate list -g jolt-prod -n cae-jolt-prod --managed-certificates-only -o table`;
  `Pending` means DNS (CNAME and `asuid` TXT) is not visible yet; once
  `Succeeded`, rerun `scripts/deploy-infra.sh --env prod --skip-build`.
- **`azure/login` fails with "AADSTS70021: No matching federated identity record":**
  the job must run in environment `dev` from repo `Dhiwakar1997/jolt-backend`;
  check `githubRepository` / `githubEnvironment` in the params file.
  For prod, the job must run in environment `prod` (`githubEnvironment = 'prod'`).
- **Deploy prod: "jolt-api:<sha> is not in <dev acr>":** the commit was never
  deployed to dev (or its image was deleted). Merge to `main` and let
  `deploy-dev.yml` build it first.
