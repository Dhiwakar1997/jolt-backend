# Jolt backend

A thin backend implementing the [Jolt LLD](./jolt-lld.md): an MCP tool surface plus
a REST API over Azure Cosmos DB, Blob Storage, and Key Vault. The core loop runs
**no inference** — storage, scheduling (FSRS), retrieval, and pure computation only.

## Topology (MVP)

**One deployable unit.** A single FastAPI app (`jolt-api`) serves both surfaces:

- `POST /mcp/sse` … — the MCP server for agents / scheduled tasks
- `/v1/*` — the REST API for the Flutter app
- `/health` — liveness

There is **no scheduler and no timed job**. FSRS recompute runs **inline**, in the
same request a grade arrives in:

- stage-2 review submission (`POST /v1/reviews/{review_id}/stage2`) folds the card's
  history including the new provisional grade, and
- agent grade submission (`jolt_submit_gradings`) applies each semantic grade and
  re-folds the affected card.

Expired sync leases are reclaimed **lazily** — the claim query treats an `in_flight`
row past its `lease_expires_at` as claimable — so no sweeper is needed either.

## Layout

```
jolt/
├── main.py            FastAPI assembly, lifespan, mounts
├── config.py          settings from env / .env (pydantic-settings)
├── runtime.py         composition root: gateways + repositories
├── mcp/               MCP tool surface (server + tools/ + descriptions/)
├── api/               REST routes + auth deps
├── services/          use-case composition over domain + data
├── domain/            business logic — no FastAPI, no Azure SDK (unit-testable)
├── data/              the only layer touching Azure (cosmos, blob, repositories, leases)
├── auth/              app + MCP tokens (issue / look up / verify), Google ID tokens, token → user
└── credentials/       per-user provider keys (vault, providers, dormant inference seam)
```

Dependency rule: `api/` + `mcp/` → `services/` → `domain/` → `data/`, nothing depends
upward. `domain/` imports neither FastAPI nor the Azure SDK.

## Configuration

All configuration comes from the environment (`pydantic-settings`). Copy the example
and fill it in **after deploying the Azure resources** — the file is git-ignored:

```bash
cp .env.example .env
# then edit .env with the endpoints / keys from your deployment
```

`.env.example` documents every variable. The essentials:

| Variable | What it is |
|---|---|
| `AUTH_MODE` | `managed_identity` (preferred) or `key` (local/emulator) |
| `COSMOS_ENDPOINT` / `COSMOS_DATABASE` / `COSMOS_KEY` | Cosmos account |
| `BLOB_ACCOUNT_URL` / `BLOB_CONTAINER` / `BLOB_CONNECTION_STRING` | Blob storage |
| `KEYVAULT_URL` | Key Vault for tokens material + per-user provider keys |
| `TOKEN_HASH_PEPPER` | server-side pepper mixed into token hashing and the token lookup index |
| `GOOGLE_OAUTH_CLIENT_ID` | Google OAuth client id(s) whose ID tokens sign-in accepts |
| `MCP_SERVER_URL` | MCP address shown in the app (`GET /v1/me`) |
| `DEV_AUTH_ENABLED` / `DEV_AUTH_SECRET` | dev-only sign-in by e-mail (`POST /v1/auth/dev`); never in prod |

With `AUTH_MODE=managed_identity`, leave the `*_KEY` / `*_CONNECTION_STRING` blank —
the app authenticates via `DefaultAzureCredential` (a Managed Identity in Container
Apps, or `az login` locally). In Azure the database, containers and blob container
are created by `infra/` (data-plane RBAC cannot create them); with `AUTH_MODE=key`
(emulator) they are created on first startup if missing.

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # fill in after deploying resources
uvicorn jolt.main:app --reload  # needs a reachable Cosmos/Blob/Key Vault (or emulator)
```

Startup fails fast with a clear message if a required endpoint is unset — that is
expected until the resources exist and `.env` is filled in.

To run with **no Azure at all**, use the in-memory fakes (state is lost on restart).
With `PUBLIC_BASE_URL` set, upload/read URLs are served by the app itself at
`/_dev/blob/...`, so the browser upload flow works too:

```bash
STORAGE_MODE=memory DEV_AUTH_ENABLED=true PUBLIC_BASE_URL=http://localhost:8000 \
    uvicorn jolt.main:app --port 8000
python scripts/smoke.py --base-url http://localhost:8000   # full round trip
```

### Signing in and connecting a model

```
POST /v1/auth/google {id_token}   -> { app_token, user }   # the app, per device
POST /v1/auth/dev    {email}      -> { app_token, user }   # dev only (DEV_AUTH_ENABLED)
POST /v1/mcp-tokens  {label}      -> { id, token, ... }    # token shown ONCE
```

The app keeps its app token (`jolt_app_…`) and sends it as `Authorization: Bearer`
on `/v1`. The MCP token (`jolt_live_…`) goes in the Claude / Codex MCP client
config as the connection's `Authorization` header; `/mcp` accepts only MCP
tokens, `/v1` only app tokens. See `docs/API.md` §1.

## Tests and checks

No Azure is needed. Domain tests are pure; API-level tests run the real FastAPI
app and MCP tools over in-memory fakes for Cosmos, Blob and Key Vault
(`jolt/data/memory.py`, fixtures in `tests/conftest.py`).

```bash
ruff check . && ruff format --check .
python -m pytest -q
python scripts/export_contract.py --check   # contract/ is up to date
```

CI (`.github/workflows/ci.yml`) runs exactly these on every push and pull request.
The suite includes `tests/test_smoke_local.py`, which runs `scripts/smoke.py`
against a real local Uvicorn server in memory mode, and `tests/test_infra.py`,
which keeps `infra/` in step with the code (Cosmos containers and indexes, env
vars, `.env.example`).

The scope-enforcement test (`tests/test_partition_discipline.py`) asserts that a
repository refuses to query without a partition value — the LLD §5 guarantee.

## API contract

`contract/openapi.json` (REST `/v1`) and `contract/mcp-tools.json` (every MCP tool:
name, description, input schema, output schema) are generated — never edit them by
hand. After changing a route, a tool, its description or `jolt/mcp/schemas.py`:

```bash
python scripts/export_contract.py
```

Human-readable reference: [`docs/API.md`](./docs/API.md). Screens without an
endpoint yet: [`docs/GAPS.md`](./docs/GAPS.md).

## Container / deploy

Single image, single entrypoint:

```bash
docker build --build-arg APP_VERSION=$(git rev-parse HEAD) -t jolt-api .
```

The `dev` environment on Azure is infrastructure as code in `infra/` (Bicep:
Container Apps, Cosmos serverless, Storage, Key Vault, Log Analytics, ACR, Managed
Identity and its role assignments) and is created or updated with one command:

```bash
scripts/deploy-infra.sh            # see docs/DEPLOY.md
```

Pushes to `main` then deploy automatically (`.github/workflows/deploy-dev.yml`:
checks → build → push to ACR → roll out → `scripts/smoke.py`). Every manual step,
setting, secret and the rollback procedure: [`docs/DEPLOY.md`](./docs/DEPLOY.md).
```
