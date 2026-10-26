# Deploying the Jolt backend (dev)

Everything in Azure is described in `infra/` (Bicep) and created by one command,
`scripts/deploy-infra.sh`. After that, every push to `main` builds, deploys and
smoke-tests automatically (`.github/workflows/deploy-dev.yml`). This page lists
every manual step and every value the owner has to provide; there are no secrets
to paste anywhere.

- [What gets created](#what-gets-created)
- [Owner checklist](#owner-checklist) (the manual steps, in order)
- [1. Prerequisites](#1-prerequisites)
- [2. Review the parameters](#2-review-the-parameters)
- [3. Deploy](#3-deploy)
- [Google sign-in](#google-sign-in)
- [4. Verify](#4-verify)
- [5. Automatic deploys from GitHub](#5-automatic-deploys-from-github)
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
| Container Registry (Basic) | `crjoltdev<sfx>` | Images `jolt-api:<git-sha>`. |
| Cosmos DB for NoSQL, **serverless** | `cosmos-jolt-dev-<sfx>` | Database `jolt`; the 12 containers (`tokens` added by BE-03), partition keys and composite indexes of LLD §6 (from `infra/cosmos-containers.json`, kept equal to `jolt/data/cosmos.py` by `tests/test_infra.py`). Account keys disabled. Periodic backup every 24 h, kept 48 h. |
| Storage account (StorageV2, LRS, Hot) | `stjoltdev<sfx>` | Private container `sources`; Blob CORS for the app origins; blob and container soft delete 7 days; shared keys disabled (uploads use user-delegation SAS). |
| Key Vault (RBAC) | `kv-jolt-dev-<sfx8>` | Secrets `token-hash-pepper` and `dev-auth-secret` (dev sign-in, BE-03); per-user provider keys `apikey-*` written by the app. Soft delete 7 days, no purge protection (dev). |
| Container Apps environment | `cae-jolt-dev` | Consumption plan. |
| Container App | `jolt-api` | External HTTPS ingress → port 8000, `/health` startup/readiness/liveness probes, scale 0..1, 0.5 vCPU / 1 GiB. |

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
10. Optional: a budget alert on the subscription (LLD §9).

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
          Microsoft.ManagedIdentity Microsoft.OperationalInsights Microsoft.Storage; do
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
(`?token=`), then revokes the MCP token and signs out (both refused afterwards).
Exit code 0 means pass. It needs the dev sign-in secret in `JOLT_DEV_AUTH_SECRET`
(or `--dev-auth-secret`); `deploy-infra.sh --smoke` and the deploy workflow
supply it. By hand, with Key Vault data access:
`export JOLT_DEV_AUTH_SECRET=$(az keyvault secret show --vault-name <kv> -n dev-auth-secret --query value -o tsv)`.
The first request after idle takes a few seconds (scale from zero); the script
waits up to `--wait` seconds (default 120) for `/health`.

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

To change a value without a code change, edit the Bicep parameter and run
`scripts/deploy-infra.sh --skip-build`. To rotate the pepper (logs every user
out): `az keyvault secret set --vault-name <kv> -n token-hash-pepper --value "$(openssl rand -base64 48)"`
then restart the app (`az containerapp revision restart`); Container Apps also
re-reads Key Vault references periodically.

**Budget alert (optional, LLD §9):**

```bash
az consumption budget create --budget-name jolt-dev-monthly --amount 20 \
  --category cost --time-grain monthly --resource-group jolt-dev \
  --start-date "$(date +%Y-%m-01)" --end-date 2027-12-31
```

(Notification e-mails are easiest to add in the portal: Cost Management → Budgets.)

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
- **Data:** Cosmos periodic backups (every 24 h, kept 48 h) are restored by
  opening a support request from the portal. Deleted blobs and containers can be
  undeleted for 7 days (`az storage blob undelete --auth-mode login …`). Deleted
  Key Vault secrets are recoverable for 7 days unless the app purged them (API-key
  deletes purge on purpose).

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
- **`azure/login` fails with "AADSTS70021: No matching federated identity record":**
  the job must run in environment `dev` from repo `Dhiwakar1997/jolt-backend`;
  check `githubRepository` / `githubEnvironment` in the params file.
