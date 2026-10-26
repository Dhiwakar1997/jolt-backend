# Security review (BE-10)

Review of the Jolt backend as of API 1.0.0 (BE-01 to BE-10 merged): code,
infrastructure (`infra/`), container image and dependencies. **BE-11 (API
1.1.0)** added the first outbound model calls, on users' own keys; they are
reviewed in [Outbound model calls (BE-11)](#outbound-model-calls-be-11),
findings S-41 to S-48. Each finding has a
severity, a status and the files involved. Every fix has a test; most are in
`tests/test_security.py`, the rest are named in the finding.

- **Severity**: high (a realistic path to another user's data or secrets),
  medium (a weakness an attacker or an accident can use, with limits), low
  (defence in depth, small impact), info (checked, nothing to fix, written
  down so the next review does not redo it).
- **Status**: fixed (in this review), accepted (a known trade-off, with the
  reason), open (worth doing, not done here, with the reason).

**No open high findings.**

## Summary

| Severity | Fixed | Accepted | Open | Total |
|---|---|---|---|---|
| High | 2 | 0 | 0 | 2 |
| Medium | 8 | 0 | 0 | 8 |
| Low | 12 | 8 | 2 | 22 |
| Info | 0 | 16 | 0 | 16 |
| **Total** | **22** | **24** | **2** | **48** |

| ID | Finding | Severity | Status |
|---|---|---|---|
| S-01 | MCP tokens from `?token=` stored in Application Insights request URLs | high | fixed |
| S-02 | MCP SDK 1.2.0: messages routed into a session by its id alone; session ids in logs | high | fixed |
| S-03 | Key Vault access was not logged | medium | fixed |
| S-04 | A failed Key Vault delete was swallowed and the reference dropped | medium | fixed |
| S-05 | App tokens never expired (GAPS #7) | medium | fixed |
| S-06 | Per-IP rate limit bypassable with a forged `X-Forwarded-For` | medium | fixed |
| S-07 | MCP tool inputs had no size limits | medium | fixed |
| S-08 | Vulnerable dependencies (Starlette, aiohttp, mcp) | medium | fixed |
| S-09 | `422` responses echoed the rejected input (API keys, ID tokens) | medium | fixed |
| S-10 | The placeholder token pepper was accepted with real storage | medium | fixed |
| S-11 | Google sign-in accepted an unverified e-mail | low | fixed |
| S-12 | No security headers on API responses | low | fixed |
| S-13 | `/docs`, `/redoc`, `/openapi.json` served in prod | low | fixed |
| S-14 | SAS URLs not HTTPS-only; downloads served with the uploader's content type | low | fixed |
| S-15 | Dev sign-in in prod guarded by the app only | low | fixed |
| S-16 | Unbounded `id_token` and API `key` fields | low | fixed |
| S-17 | Uvicorn `server` header | low | fixed |
| S-18 | `/_dev/blob` (memory mode) served uploads on the API origin | low | fixed |
| S-19 | Nightly prompt did not say note content is data | low | fixed |
| S-20 | `?token=` MCP sessions cannot be bound to the token | low | accepted |
| S-21 | MCP token in a connector URL | low | accepted |
| S-22 | Upload SAS: overwrite within its lifetime, no size cap at PUT | low | accepted |
| S-23 | Key Vault, Cosmos and Storage reachable from public networks | low | accepted |
| S-24 | One app identity with vault- and account-wide data roles | low | accepted |
| S-25 | MCP tool errors carry exception text to the user's agent | low | accepted |
| S-26 | `DefaultAzureCredential` rather than `ManagedIdentityCredential` | low | accepted |
| S-27 | No "signed-in devices" list | low | open |
| S-28 | Base image not pinned by digest, no image scan (pip-audit now in CI) | low | open |
| S-29 | Token hashing and lookup | info | accepted |
| S-30 | Token kinds per surface, sign-out, revocation | info | accepted |
| S-31 | Google ID token verification | info | accepted |
| S-32 | Ownership on every route and MCP tool | info | accepted |
| S-33 | Curated global read path | info | accepted |
| S-34 | Account deletion completeness | info | accepted |
| S-35 | Maintenance job safety | info | accepted |
| S-36 | Data export | info | accepted |
| S-37 | CORS | info | accepted |
| S-38 | Container image | info | accepted |
| S-39 | Stored content shown back to users; prompt injection | info | accepted |
| S-40 | Backups keep deleted data for a while | info | accepted |
| S-41 | Key custody on the model-call path | info | accepted |
| S-42 | The free path makes no outbound call | info | accepted |
| S-43 | A stolen app token could spend a user's provider credit | low | fixed |
| S-44 | Notes and answers are sent to a third-party provider | low | accepted |
| S-45 | Outbound targets are fixed by the server (no SSRF) | info | accepted |
| S-46 | Prompt injection through notes and answers | info | accepted |
| S-47 | Unbounded model inputs and outputs | low | fixed |
| S-48 | Provider error bodies can echo the key | low | fixed |

---

## High

### S-01 MCP tokens from `?token=` stored in Application Insights request URLs — fixed

Claude's custom connectors take only a URL, so `GET /mcp/sse?token=jolt_live_…`
carries the MCP token in the query (`MCP_QUERY_TOKEN_ENABLED`, BE-04). The
Uvicorn access log already redacted it, but the OpenTelemetry FastAPI
instrumentation (BE-10 item 2) records the full URL, query included, as the
request span's `http.url` / `http.target` (`url.full` / `url.query` in the
newer conventions). Those land in the App Insights `requests` table for 90 days
(prod), readable by anyone with reader access to the workspace, and the token
opens that user's whole MCP surface.

Fix: `redact_span` (`jolt/api/security.py`) is the instrumentation's
`server_request_hook` (`jolt/telemetry.py`, `instrument_app`): it rewrites the
URL attributes of every request span with `token=` and `session_id=` values
blanked before export. The access-log filter uses the same helper
(`jolt/mcp/server.py`, `RedactTokenFilter`). Only Jolt's own `jolt.*` loggers
are exported, never the access log.
Tests: `test_request_spans_sent_to_app_insights_are_redacted`,
`test_redaction_covers_token_and_session_id`, `test_access_log_redacts_session_ids`.

### S-02 MCP SDK 1.2.0: messages routed into a session by its id alone; session ids in logs — fixed

With HTTP+SSE, a client opens `GET /mcp/sse` and then posts its messages to
`/mcp/messages/?session_id=<id>`. Tool calls run in the context of the
*stream* (the token that opened it), and mcp 1.2.0 accepted a message for any
session whose id it knew, whatever credential the POST carried
(GHSA-jpw9-pfvf-9f58, fixed in 1.27.2). The id is 128 random bits sent only to
the stream, but it was written to the access log and to the telemetry request
URLs on every POST, so anyone who could read logs could inject tool calls
(store or rewrite concepts, read notes) into a live user's nightly sync.

Fix:
- `mcp` 1.2.0 → 1.30.0 (`requirements.txt`; Pydantic 2.10 → 2.12). The SSE
  transport now remembers the credential that opened a session and answers
  `404` to messages with any other. `jolt/mcp/server.py` gives it that
  credential: for a token from the `Authorization` header, `scope["user"]` is
  set to the token's SHA-256 key (never the token). The message endpoint is now
  relative to the mount (the new transport prefixes `root_path`).
- `session_id` values are redacted from the access log and telemetry (S-01).

Tests: `test_mcp_session_accepts_messages_only_from_its_own_credential` (a live
server: no credential and another user's token get `404`, the owner's `202`),
the existing MCP suites (`tests/test_mcp_flow.py`, `tests/test_smoke_local.py`)
on the new SDK. Residual risk for `?token=` sessions: S-20.

## Medium

### S-03 Key Vault access was not logged — fixed

The brief asks for logged Key Vault access; the vault had no diagnostic
setting, so reads of the pepper and of users' provider keys left no trace
outside the control plane.
Fix: `infra/modules/keyvault.bicep` adds a diagnostic setting sending the
`audit` category group (AuditEvent: every secret get / set / delete / purge,
with the caller's identity, result and client IP) to the Log Analytics
workspace (`infra/main.bicep` passes `monitoring.outputs.id`). Kept for the
workspace retention (30 days dev, 90 prod). Query:
`AzureDiagnostics | where ResourceProvider == "MICROSOFT.KEYVAULT"`.
Test: `test_key_vault_access_is_logged`; Bicep build and lint pass.
**Deploy pending**: the first rows appear after the next deploy.

### S-04 A failed Key Vault delete was swallowed and the reference dropped — fixed

`VaultClient.delete_key` (`jolt/credentials/vault.py`) caught every exception
from the delete and from the purge. A refused delete (permissions, throttling,
an outage) then let `CredentialService.delete` and account delete remove the
Cosmos reference while the user's provider key stayed in the vault, readable,
with nothing pointing at it.
Fix: only "not found" is tolerated on delete; any other error is raised, so
the reference stays and the call (or the account delete) can be retried. A
failed purge (purge protection, a delete still settling) is logged with the
secret name and status, never the value; the secret is deleted and unreadable
and expires with soft-delete retention.
Tests: `test_vault_delete_purges_and_tolerates_missing_secrets`,
`test_failed_vault_delete_keeps_the_cosmos_reference`.

### S-05 App tokens never expired (GAPS #7) — fixed

A lost or stolen device's app token stayed valid until the pepper rotated
(which signs everyone out).
Decision: **app tokens expire after `APP_TOKEN_IDLE_DAYS` (30) days without a
request; MCP tokens do not expire but can be revoked.** Every request renews an
app token (`last_used_at`, written at most once a minute), so an app in use is
never signed out; a nightly client may legitimately sit idle for weeks, and its
token only opens `/mcp`, is one per client and is revoked in one tap.
Implementation: `authenticate` (`jolt/auth/context.py`) refuses an idle app
token with `401 "token expired; sign in again"` and deletes it; the
maintenance job (`jolt/services/maintenance.py`) sweeps the expired ones daily
(`TokensRepository.list_kind_across_users`). `0` disables expiry.
Tests: `test_idle_app_tokens_expire_and_are_deleted`,
`test_never_used_app_token_expires_from_issue_and_zero_disables`,
`test_mcp_tokens_do_not_expire`, `test_maintenance_deletes_expired_app_tokens_only`.
A device list is still open (S-27).

### S-06 Per-IP rate limit bypassable with a forged `X-Forwarded-For` — fixed

Uvicorn runs with `--proxy-headers --forwarded-allow-ips "*"` behind the
Container Apps ingress and sets the client address to the *leftmost*
`X-Forwarded-For` entry, which the client writes. Sending a random value per
request gave every request a fresh per-IP bucket, defeating
`RATE_LIMIT_IP_PER_MINUTE` (the cap that covers token-less and random-token
floods).
Fix: `_client_ip` (`jolt/api/limits.py`) takes the entry `TRUSTED_PROXY_HOPS`
places from the right (the one the ingress appends); Bicep sets it to `1`
(`infra/modules/containerapp.bicep`). `0` (local) keeps the TCP peer.
Tests: `test_client_ip_uses_the_trusted_hop_of_x_forwarded_for`,
`test_spoofed_x_forwarded_for_does_not_reset_the_ip_limit`,
`test_container_app_trusts_one_proxy_hop`.

### S-07 MCP tool inputs had no size limits — fixed

Only the 4 MiB body cap bounded a tool call. A string over Cosmos's 2 MB item
limit failed half way through a call; a few hundred kilobytes of tiny concepts
was thousands of Cosmos writes (RU cost, and a slow call holding the sync
lease).
Fix: `jolt/mcp/input_limits.py`, applied to every tool call before it runs
(`jolt/mcp/server.py`): at most 200 concepts per call, 500 syllabus items, 20
questions, 10 options, 100 gradings, 50 source ids, 100 of any other list;
`markdown` up to 500,000 characters, ids and keys 200, other text 20,000;
`limit` 1 to 200. Over a limit the call fails with `input too large: …` and
writes nothing. The prompt already sends at most a step's `limit` per call.
Tests: `test_check_tool_inputs_limits`, `test_oversized_tool_calls_fail_before_writing`.

### S-08 Vulnerable dependencies — fixed

`pip-audit -r requirements.txt` (pip-audit 2.x, run in this review) reported:
- **Starlette 0.41.3** (via FastAPI 0.115.6): Host-header URL reconstruction
  (CVE-2026-48710), request-path URL reconstruction (CVE-2026-54282), form
  limits and Range-header DoS, `HTTPEndpoint` method dispatch. → FastAPI
  0.162.0 with **Starlette 1.10.0** pinned explicitly.
- **aiohttp 3.12.15** (the Azure SDK's async transport; client side only):
  about 40 advisories, mostly server-side parsers Jolt does not run, plus client
  decompression, cookie and redirect handling. → **3.14.5**.
- **mcp 1.2.0**: five advisories (S-02; validation-error 500s; Streamable HTTP
  and WebSocket transports and DNS rebinding, which Jolt does not use). →
  **1.30.0** (1.28.1 still had CVE-2026-59951, Streamable HTTP only).

After the upgrade `pip-audit -r requirements.txt` reports **no known
vulnerabilities**. The full suite passes on the new versions. CI now runs
pip-audit on every push (S-28).

### S-09 `422` responses echoed the rejected input — fixed

FastAPI's validation error body repeats each rejected value as `input`. A
`POST /v1/settings/api-key` without `provider` sent the provider API key back
in the response; a malformed sign-in did the same with the ID token. Only the
caller saw it, but keys must never appear in errors (and response bodies end
up in client logs and crash reports).
Fix: `validation_error_handler` (`jolt/api/security.py`) answers the same
`{"detail": [{loc, msg, type, ctx?}]}` without `input`; the OpenAPI
`ValidationError` schema no longer lists it.
Test: `test_validation_errors_do_not_echo_secrets`.

### S-10 The placeholder token pepper was accepted with real storage — fixed

`TOKEN_HASH_PEPPER` defaults to the `.env.example` placeholder. Bicep always
sets it from Key Vault, but a misconfigured deployment (or a local run against
Azure) would have hashed real tokens with a public pepper, which makes the
lookup HMAC and the Argon2 pepper worthless.
Fix: `Settings.check_serving_secrets` (`jolt/config.py`), called when the API
starts (`jolt/main.py` lifespan), refuses the placeholder or anything under 32
characters when `STORAGE_MODE=azure`. The maintenance job reads no tokens and
gets no pepper, so the check is not in `Settings` itself.
Tests: `test_api_refuses_a_placeholder_or_short_pepper_with_azure_storage`,
`test_pepper_check_passes_for_a_real_pepper_and_memory_mode`.

## Low

### S-11 Google sign-in accepted an unverified e-mail — fixed

Accounts are keyed on Google's `sub`, so this never merged accounts, but the
e-mail is shown, exported and used as the display-name fallback.
Fix: `GoogleIdTokenVerifier.verify` (`jolt/auth/google.py`) refuses a token
with an e-mail and without `email_verified` true (the legacy string `"true"`
is accepted). Tests: `test_google_sign_in_rejects_more_bad_claims`,
`test_google_sign_in_accepts_legacy_string_email_verified`.

### S-12 No security headers on API responses — fixed

`SecurityHeadersMiddleware` (`jolt/api/security.py`, outside the limits so a
`429` has them too): `X-Content-Type-Options: nosniff`, `X-Frame-Options:
DENY`, `Referrer-Policy: no-referrer`, `Cross-Origin-Resource-Policy:
same-site`, `Content-Security-Policy: default-src 'none'; frame-ancestors
'none'` (not on the dev-only docs pages, which need their scripts),
`Cache-Control: no-store` on `/v1` and `/mcp` (tokens and personal data), and
HSTS (one year) in prod. The deploy smoke test checks them
(`scripts/smoke.py --public`).
Tests: `test_api_responses_carry_security_headers`, `test_hsts_in_prod_and_headers_on_a_429`,
`tests/test_smoke_local.py::test_public_smoke_passes_against_a_prod_like_server`.

### S-13 `/docs`, `/redoc`, `/openapi.json` served in prod — fixed

Decision: off in prod, on elsewhere (`API_DOCS_ENABLED` overrides). The
contract is public in `contract/openapi.json`, so this hides nothing secret;
it removes the Swagger UI (third-party scripts from a CDN on the API origin)
and a free map of the surface. Tests: `test_docs_and_openapi_are_off_in_prod_and_on_in_dev`,
the public smoke step "GET /docs and /openapi.json -> 404".

### S-14 SAS URLs not HTTPS-only; downloads served with the uploader's content type — fixed

`jolt/data/blob.py`: every SAS now carries `spr=https` (the account already
refuses HTTP, `supportsHttpsTrafficOnly`), and page read URLs carry
`rsct=<declared type>`, one of the accepted image/PDF types, so a file PUT as
`text/html` is never served as a page from the storage host. Scope and
lifetime, checked: one blob per URL (`sr=b`); uploads create + write only
(`sp=cw`), 15 minutes (`BLOB_SAS_TTL_SECONDS`); page reads read only, 15
minutes; the export download read only, 1 hour (`EXPORT_URL_TTL_SECONDS`,
capped at 7 days), served as an attachment; signed with a user-delegation key
valid only for the SAS's lifetime (shared keys are disabled). Blob paths are
built from server ids and a filename reduced to `[A-Za-z0-9._-]`
(`page_blob_path`), so `../` cannot leave `<user>/<source>/`.
Tests: `test_sas_urls_are_https_only_single_blob_and_short_lived`,
`test_blob_paths_cannot_escape_the_users_source_folder`.

### S-15 Dev sign-in in prod guarded by the app only — fixed

`Settings` already refuses `DEV_AUTH_ENABLED` with `JOLT_ENV=prod`
(`jolt/config.py`, test `test_dev_sign_in_is_refused_by_config_where_unsafe`),
the prod parameter file sets it false (`test_prod_parameters_are_strict`) and
the prod smoke test checks `POST /v1/auth/dev` is 404. Added: `infra/main.bicep`
computes `devAuth = devAuthEnabled && environmentName != 'prod'`, so not even a
wrong parameter file can turn it on in prod (and the GitHub identity is not
granted the dev secret there). Test: `test_dev_sign_in_cannot_reach_prod_through_bicep`.

### S-16 Unbounded `id_token` and API `key` fields — fixed

`id_token` is capped at 8,192 characters, `key` at 512 and `provider` at 32
(`jolt/api/routes/auth.py`, `jolt/api/routes/settings.py`), before any
verification or provider call. Test: `test_validation_errors_do_not_echo_secrets`.

### S-17 Uvicorn `server` header — fixed

`--no-server-header` in the `Dockerfile` CMD. Test:
`test_image_runs_unprivileged_and_carries_no_secrets`.

### S-18 `/_dev/blob` (memory mode) served uploads on the API origin — fixed

Memory mode only (never Azure: the routes are not mounted with
`STORAGE_MODE=azure`, `test_dev_blob_routes_absent_outside_memory_mode`), but a
local dev server served an uploaded file with the uploader's content type from
the API origin. The CSP `default-src 'none'` and `nosniff` from S-12 now apply
to those responses, so nothing in them can run.

### S-19 Nightly prompt did not say note content is data — fixed

See S-39. `docs/NIGHTLY_SYNC_PROMPT.md` (served by `GET /v1/sync/prompt`,
version `2026-10-26.5`) now tells the model that text in a note that reads
like instructions is transcribed, never acted on.

### S-20 `?token=` MCP sessions cannot be bound to the token — accepted

A client that connects with `?token=` posts its messages to the announced
`/mcp/messages/?session_id=…` without a credential, so its session cannot be
bound (S-02); anyone holding the session id could post into it. Accepted: the
id is 128 random bits, sent only to the stream over TLS, redacted from the
access log and telemetry, and dies with the stream; a message with another
user's token is still refused. Header clients (Codex, Claude Desktop config,
`mcp-remote`) are bound. Remove with OAuth for connectors (`CONNECT.md`
follow-ups) or by turning `MCP_QUERY_TOKEN_ENABLED` off once no client needs it.
Note: the SDK's own warning lines for unknown or refused sessions
(`mcp.server.sse`) print that session id to the console log.

### S-21 MCP token in a connector URL — accepted

The token sits in the client's connector settings. Accepted (`CONNECT.md`):
an MCP token only opens `/mcp`, is one per client, never expires silently but is
revoked in one tap, and is redacted server side (S-01). Header auth is
documented as the preferred setup.

### S-22 Upload SAS: overwrite within its lifetime, no size cap at PUT — accepted

The upload URL has write permission for 15 minutes, so the uploader can
replace the file after confirm; and a SAS cannot cap the PUT size. Accepted:
both affect only the uploader's own note, read by their own model. Confirm
checks size and SHA-256 and deletes oversize files; unconfirmed uploads are
swept after 24 h (`jolt/services/maintenance.py`); a Blob capacity alert
exists (`infra/modules/alerts.bicep`).

### S-23 Key Vault, Cosmos and Storage reachable from public networks — accepted

No private endpoints (`publicNetworkAccess: 'Enabled'` in
`infra/modules/keyvault.bicep`, `storage.bicep`, `cosmos.bicep`). Every data
call needs an Entra token with a role assignment; Cosmos account keys and
Storage shared keys are disabled (`allowKeyAuth: false`); Key Vault is RBAC
with access logged (S-03). Private endpoints need a VNet-integrated Container
Apps environment (cost and complexity out of proportion for the MVP).

### S-24 One app identity with vault- and account-wide data roles — accepted

`id-jolt-<env>-api` is Key Vault Secrets Officer on the vault (it reads the
pepper and sets, reads, deletes and purges every `apikey-*`) and Storage Blob
Data Contributor on the account; the maintenance job uses the same identity.
Accepted for a single-service MVP. Later: a separate job identity without Key
Vault access, and Secrets User on the pepper only plus a custom role for
`apikey-*` (Key Vault RBAC cannot scope a role to a name prefix).

### S-25 MCP tool errors carry exception text to the user's agent — accepted

An unexpected exception in a tool reaches the caller's own agent as an MCP
tool error and is noted on their sync run (`jolt/mcp/server.py`); a Cosmos
error's text can include an activity id or status. Only the token's owner sees
it; REST `500`s are bare `Internal Server Error` with no trace.

### S-26 `DefaultAzureCredential` rather than `ManagedIdentityCredential` — accepted

`jolt/data/cosmos.py`, `jolt/data/blob.py`, `jolt/credentials/vault.py`. In
Container Apps no environment credential is configured, so the chain resolves
to the user-assigned identity (`AZURE_CLIENT_ID`); the same code runs locally
with `az login`. No secret credential can be picked up because none exists.

### S-27 No "signed-in devices" list — open

App tokens are not listed, so a user cannot sign out one lost device
(GAPS #7); idle expiry (S-05) and account delete bound the exposure. Open:
needs an API and a Settings screen nobody has designed; do it if asked for.

### S-28 Base image not pinned by digest, no image scan in CI — open

`FROM python:3.12-slim` floats with upstream patches (good for OS fixes, but a
build is not reproducible), and nothing scans the built image. Done in this
review: a `Dependency audit` job in `.github/workflows/ci.yml` runs
`pip-audit -r requirements.txt` on every push, so a known vulnerability in a
pinned Python dependency fails CI and therefore the deploy. Open: pin the base
image by digest with Dependabot updates and add an image scan (e.g. Trivy);
left out because they need repository settings (Dependabot, code scanning)
only the owner can turn on.

## Info (checked)

### S-29 Token hashing and lookup

`jolt/auth/tokens.py`: 256-bit random tokens (`secrets.token_urlsafe(32)`)
with a kind prefix; stored as Argon2id (argon2-cffi defaults) of token +
pepper, plus a 128-bit HMAC-SHA256 lookup key keyed by the pepper (one indexed
read and one Argon2 verify per request, no scan, `test_token_lookup_verifies_one_hash_not_every_user`).
A random token finds no document and costs no Argon2. Plaintext is returned
once and never stored or logged; exports list metadata only. The pepper is a
Key Vault reference, generated by `scripts/deploy-infra.sh`.

### S-30 Token kinds per surface, sign-out, revocation

`/v1` accepts app tokens only and `/mcp` MCP tokens only
(`test_v1_rejects_an_mcp_token`, `test_mcp_rejects_an_app_token`). Sign-out
deletes the calling app token; revoking an MCP token cuts an open connection
at its next tool call (`test_mcp_token_revocation_takes_effect_immediately`).
Rate-limit buckets key on a SHA-256 of the token, never the token.

### S-31 Google ID token verification

`jolt/auth/google.py`: the header's `alg` must be RS256 (HS256 and `none` are
refused before any key is used, `test_google_sign_in_rejects_a_symmetric_token`);
the key comes from Google's JWKS by `kid`; `iss` is one of Google's two
issuers; `aud` must be one of `GOOGLE_OAUTH_CLIENT_ID` (a list, for web and
mobile); `exp`, `iat`, `iss`, `aud`, `sub` are required; `exp`, `iat` and
`nbf` are checked with 30 s leeway; the e-mail must be verified (S-11).

### S-32 Ownership on every route and MCP tool

`jolt/services/ownership.py` (BE-07, decision A16) answers "is this the
caller's?" for every track-, concept-, question- and source-keyed call; failures
look like unknown ids. `tests/test_ownership.py` covers every MCP tool and
every keyed REST route; this review added the BE-08 / BE-09 routes (library,
decision tree, enrol / unenrol, review-session end, MCP token delete) and
checks that aggregate views (progress, storage, sync runs and status, due
reviews, settings, tokens, sources, catalog, export) show nothing of another
user: `test_be08_be09_routes_refuse_another_users_ids_and_show_nothing_of_theirs`.
No route or tool takes a user id.

### S-33 Curated global read path

Curated tracks live in a bucket no user partition reaches (`user_id: null`).
Reads accept them, writes do not (`owned_track`, `writable_card`). The library
and decision tree return only Jolt's `curated` concepts
(`CuratedService.library`); a curated track's detail shows each learner only
concepts they hold a card for (`visible_concepts`); `pattern_match` questions
mask the answer until stage two and are answerable only by enrolled learners
(`tests/test_curated_dsa.py`). No learner can create a concept on a curated
track (`log_session` and reconciliation need an owned track).

### S-34 Account deletion completeness

`AccountDataService.delete_account` (`jolt/services/account_data.py`): Key
Vault secrets (every supported provider, deleted and purged; with S-04 a
refused delete now stops the account delete so it can be retried), every blob
under `<user>/` and `exports/<user>/`, every document in all 14 containers
(including the track-, concept-, source- and review-partitioned ones reached
through the user's own documents), tokens, then the user. Curated content
stays. Tested by `test_delete_account_leaves_nothing_behind` (every container
and the blob and vault fakes are empty of the user) and
`test_delete_keeps_curated_tracks`. Deleted data still sits in backups for a
while (S-40).

### S-35 Maintenance job safety

`jolt/services/maintenance.py`: deletes only (1) sources never confirmed after
`ABANDONED_UPLOAD_HOURS`, (2) sources older than a user's own
`data_retention_days` (1 to 3650; unset keeps everything), (3) expired app
tokens. Blob deletes are by the prefix `<user_id>/<source_id>/`, both
server-generated ids, so a sweep cannot reach another user's or source's
files. Idempotent, with `--dry-run`, no HTTP route, and a failure exits
non-zero. Same identity as the app (S-24).

### S-36 Data export

The bundle (`AccountDataService.bundle`) drops token hashes, lookup keys, Key
Vault references, blob paths, etags and the auth subject
(`test_export_bundle_has_everything_and_no_secrets`). It is written under
`exports/<user>/` (the previous one deleted first), downloaded through a 1-hour
read-only SAS as an attachment, removed by account delete and by a 1-day Blob
lifecycle rule.

### S-37 CORS

`jolt/main.py`: exact origins (`CORS_ORIGINS`) plus an optional regex
(`CORS_ORIGIN_REGEX`, Netlify deploy previews in dev only); `allow_credentials`
false, since the token is a header and there are no cookies, so a hostile page
gains nothing from a permissive rule. Prod lists only the production HTTPS
origin, no localhost, no regex (`test_prod_parameters_are_strict`). Blob CORS
allows the same exact origins, GET / HEAD / PUT only.

### S-38 Container image

`Dockerfile`: runs as uid 10001 (`USER 10001`); only `jolt/` and the nightly
prompt are copied; `.dockerignore` excludes `.env*`, `.git`, tests, infra and
docs; no secret is an `ENV` or `ARG` (all come from Container Apps at run time,
the pepper as a Key Vault reference); pip runs without cache. Container Apps
ingress terminates TLS (`allowInsecure: false`). Test:
`test_image_runs_unprivileged_and_carries_no_secrets`.

### S-39 Stored content shown back to users; prompt injection

Everything the agent writes (read markdown, concept text, questions, feedback)
is stored as given and returned as JSON. The API never renders it, and JSON
responses carry `nosniff` and a `default-src 'none'` CSP. The front end renders
markdown safely (no raw HTML), which is where an injected `<script>` would
matter; keep it that way.

Prompt injection: the nightly prompt has the user's own model read the user's
own uploads through the user's own MCP token. A note crafted to steer the
model can at worst make it misuse *that user's* tools on *that user's* data:
every tool is scoped to the token's user (S-32), curated content is read-only,
and no tool sends data outside Jolt or reaches another account. The trust
boundary is therefore the user's account; S-19 adds a line to the prompt so
the model treats note text as content.

### S-40 Backups keep deleted data for a while

After a source, an API key or an account is deleted: blob soft delete keeps
files 7 days (dev) / 14 days (prod, plus versions for the same time), Cosmos
backups keep documents 48 h (dev, periodic) / 30 days (prod, continuous), Key
Vault soft delete keeps a deleted pepper 90 days in prod (API keys are purged
at once, S-04). Accepted: these are the restore windows (`DEPLOY.md`, "Backups
and restore"); the privacy policy should state them.

---

## Outbound model calls (BE-11)

BE-11 adds the only calls Jolt makes to a model: `POST
/v1/concepts/{id}/feedback` and `POST /v1/sources/{id}/read-now`, billed to the
user's own Anthropic or OpenAI key (`jolt/credentials/inference.py`,
`providers.py`, `jolt/services/feedback.py`, `read_now.py`, `spend.py`). Every
protection above stays as it was: keys only in Key Vault (S-03, S-04), never
in a response or a `422` (S-09), size limits (S-07, S-16), ownership on every
route (S-32), redacted telemetry (S-01).

**Egress is unchanged.** Container Apps already allows outbound HTTPS (key
validation at capture has called the providers since BE-03); no firewall,
NAT or private endpoint was added or needed. The calls go to
`ANTHROPIC_API_BASE` / `OPENAI_API_BASE` (defaults `https://api.anthropic.com`,
`https://api.openai.com`) over TLS with certificate verification (httpx
defaults), with a per-call timeout (45 s feedback, 150 s read now).

### S-41 Key custody on the model-call path — accepted (info)

`call_model` reads the plaintext from Key Vault (Managed Identity) at the
moment of the call, holds it in a local variable, puts it only in the
provider's auth header (`x-api-key` / `Authorization: Bearer`) and deletes the
reference when the call ends. It is never cached (the feedback cache holds the
model's answer, not the key), never in Cosmos, never in a log line (the
`model call` line carries provider, kind and outcome; httpx's own INFO line
carries the URL only), never in a metric attribute (`jolt.model.calls`,
`jolt.model.tokens`: provider, kind, outcome, direction), never in a span
(the outbound client is not instrumented and headers are never recorded),
never in an error (S-48). Test:
`tests/test_key_backed.py::test_the_key_never_appears_in_logs_errors_telemetry_or_documents`
(logs at DEBUG, every response body, every Cosmos document and blob including
the export, every metric attribute; only the vault fake holds the key).

### S-42 The free path makes no outbound call — accepted (info)

Without a stored key (or without one for the provider named), both routes
answer `403 key_required` right after the ownership check: `resolve_provider`
reads only the Cosmos key references, before Key Vault, the blob or any
provider. The source is not claimed either. Test:
`test_without_a_key_no_route_attempts_an_outbound_call` (the real network
transport is replaced by one that records and fails; it sees zero requests),
`test_a_key_for_the_other_provider_is_not_used`.

### S-43 A stolen app token could spend a user's provider credit — fixed (low)

An app token now unlocks paid calls on the user's key. Fix: a per-user daily
spend guard (`jolt/services/spend.py`): `MODEL_DAILY_REQUEST_CAP` (50) requests
and `MODEL_DAILY_TOKEN_CAP` (2,000,000) tokens per UTC day, counted on the user
document, a request reserved (etag-guarded) before it is sent so concurrent
calls cannot pass the cap; over it `429 spend_limit` until midnight UTC. The
per-token REST rate limit (S-06) and app token expiry (S-05) still apply, and
the feedback cache makes repeated asks free. A cap of 0 turns the features off
for everyone. Tests: `test_spend_guard_caps_requests_per_day`,
`test_spend_guard_caps_tokens_and_zero_turns_it_off`.

### S-44 Notes and answers are sent to a third-party provider — accepted (low)

Read now sends the note's original pages, feedback the concept text and the
learner's answers, to the provider whose key the user stored. Accepted: the
user opts in twice (adding a key, pressing the button), the data goes to their
own account with that provider under its terms, and only the one note or
concept is sent. OpenAI calls set `store: false`. The privacy policy and the
FE-12 copy should say that these features send the note to the user's model
provider.

### S-45 Outbound targets are fixed by the server (no SSRF) — accepted (info)

No request takes a URL: the provider base URLs are server settings, the files
are read from Blob by server-built paths of the caller's own source
(`<user>/<source>/<page>-<name>`, S-14), and the request body names only a
provider from a fixed list. A page's real type is checked from its first bytes
(`sniff`) before it is sent, so a file uploaded as `image/jpeg` that is not an
image is refused (`415`), not forwarded.

### S-46 Prompt injection through notes and answers — accepted (info)

A crafted note or answer can steer only that user's own read or feedback: the
model has no tools, both prompts state the learner's material is data, and the
result is validated and stored like an agent's (S-39: never rendered by the
API). Read now writes only to the caller's own track, through the same
ownership-checked services as `jolt_store_extraction`.

### S-47 Unbounded model inputs and outputs — fixed (low)

Inputs: images up to `READ_NOW_MAX_IMAGE_BYTES` (3.75 MB) each, a note up to
`READ_NOW_MAX_TOTAL_BYTES` (20 MiB), checked on the stored sizes before the
download and again on the bytes (`413 source_too_large`); feedback sends at
most 20 answers, each cut at 1,500 characters. Outputs: `max_tokens` per call
(`MODEL_*_MAX_TOKENS`), and the stored answer is bounded like an MCP call
(S-07): markdown 500,000 characters, 200 concepts, 3 questions of exactly 4
options each, texts 20,000 characters, feedback 2,000. Tests:
`test_read_now_size_and_type_limits`, `test_request_validation_of_the_key_backed_bodies`.

### S-48 Provider error bodies can echo the key — fixed (low)

OpenAI's `401` message repeats part of the key ("Incorrect API key provided:
sk-…"). Provider bodies are read only to classify the error (out of credit or
rate limit) and never repeated: every error is one of Jolt's own sentences with
a `code` (`ModelCallError`), and as a second guard a detail containing the key
is masked. Tests: `test_an_invalid_key_is_key_invalid_and_never_echoed`,
`test_adapters_map_statuses_without_repeating_the_body`.

**Deploy pending:** with real keys after the dev deploy, confirm that
`jolt.model.*` metrics and `model call` traces reach Application Insights
without key material, and that Key Vault audit logs (S-03) show one secret get
per model call.

---

## How this review was done

- Read every module under `jolt/`, `infra/`, the `Dockerfile`, the workflows
  and `scripts/smoke.py`; traced each REST route and MCP tool through
  `jolt/services/ownership.py`.
- `pip-audit -r requirements.txt` before and after (S-08).
- `ruff check .`, `ruff format --check .`, `pytest` (all pass),
  `python scripts/export_contract.py --check`, and Bicep `build`, `lint` and
  `build-params` for `main.dev.bicepparam` and `main.prod.bicepparam`.

Re-run this review when an auth path, a token kind, a new data store, or a
route that reads across users is added.
