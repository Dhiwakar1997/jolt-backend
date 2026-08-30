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
├── auth/              opaque-token issue / verify, token → user resolution
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
| `TOKEN_HASH_PEPPER` | server-side pepper mixed into token hashing |

With `AUTH_MODE=managed_identity`, leave the `*_KEY` / `*_CONNECTION_STRING` blank —
the app authenticates via `DefaultAzureCredential` (a Managed Identity in Container
Apps, or `az login` locally). Databases, containers, and the blob container are
created on first startup if missing.

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # fill in after deploying resources
uvicorn jolt.main:app --reload  # needs a reachable Cosmos/Blob/Key Vault (or emulator)
```

Startup fails fast with a clear message if a required endpoint is unset — that is
expected until the resources exist and `.env` is filled in.

### Bootstrapping a user

```
POST /v1/accounts            -> { user_id, token }   # token shown ONCE
```

Put the token in the MCP client config (connection `Authorization` header) and in the
app's secure storage (`Authorization: Bearer <token>`).

## Tests

Domain and repository-discipline tests need no Azure:

```bash
python -m pytest -q
```

The scope-enforcement test (`tests/test_partition_discipline.py`) asserts that a
repository refuses to query without a partition value — the LLD §5 guarantee.

## Container / deploy

Single image, single entrypoint:

```bash
docker build -t jolt-api .
```

Deploy as one Container App with Managed Identity granted data-plane roles on Cosmos,
Blob, and Key Vault; inject secrets as Key Vault env references. There is no second
job resource in this MVP.
```
