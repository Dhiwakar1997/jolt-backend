# Jolt API reference

The human-readable view of the contract. The machine-readable source of truth is
generated and checked in CI:

- [`contract/openapi.json`](../contract/openapi.json) — REST, every `/v1` route.
- [`contract/mcp-tools.json`](../contract/mcp-tools.json) — every MCP tool: name,
  full agent-facing description, input schema, output schema.

Regenerate both with `python scripts/export_contract.py` after changing a route, a
tool, a tool description or `jolt/mcp/schemas.py`. This file is written by hand:
update it in the same change.

State: API version 0.4.0: the MVP, the track agenda model (PR #3), accounts,
Google sign-in and MCP token management (BE-03), and the nightly sync: the
`jolt_sync` plan, sync runs, sync status and the scheduled-task prompt (BE-04). Screens that have no endpoint yet
are listed in [`GAPS.md`](./GAPS.md).

## Conventions

| | |
|---|---|
| **Base paths** | REST under `/v1`. MCP over HTTP+SSE: connect to `/mcp/sse`, post to `/mcp/messages/`. `GET /health` → `{status: "ok", version}` (no auth). CORS: the origins in `CORS_ORIGINS` / `CORS_ORIGIN_REGEX`, no credentials (the token is a header). |
| **Auth** | `Authorization: Bearer <token>`. Two opaque token kinds: an **app token** (`jolt_app_…`, one per signed-in device, from sign-in) on every `/v1` route except the sign-in routes, and an **MCP token** (`jolt_live_…`, one per connected client, from `POST /v1/mcp-tokens`) on the MCP connection. Each surface refuses the other kind with `401` (`detail: "wrong token kind: …"`); opening `/mcp/sse` with a missing, revoked or wrong-kind token is a `401` before the stream starts. On `/mcp/sse` only, the MCP token may instead ride the URL as `?token=` (for connectors that take only a URL; `MCP_QUERY_TOKEN_ENABLED`, redacted from access logs); client setup in [`CONNECT.md`](./CONNECT.md). Tokens do not expire; revoking (sign-out, `DELETE /v1/mcp-tokens/{id}`) takes effect on the next request. A bare token without `Bearer` is also accepted. |
| **Scope** | Identity comes only from the token. No route or tool takes a `user_id`. |
| **Errors (REST)** | Non-2xx bodies are `{"detail": "<message>"}` (`ErrorResponse`). Request-validation failures are `422` with `{"detail": [ {loc, msg, type} ]}` (`HTTPValidationError`). `401` always carries `WWW-Authenticate: Bearer`. |
| **Errors (MCP)** | Tools marked *`{error}` on* return `{"error": "<message>"}` for the listed recoverable cases. Every other failure (missing/invalid token, unknown source, grade outside 1–4) is raised and arrives as an MCP tool error (`isError: true`). |
| **Types** | IDs are opaque strings (32-hex today). Timestamps are ISO-8601 UTC. FSRS grades: 1 Again, 2 Hard, 3 Good, 4 Easy. |
| **Agenda** | `{status: none\|draft\|refined\|locked, source: agent\|user\|emergent, syllabus: [{concept_key, label, parent}], last_refined_at, refined_from_source_id}`; written `Agenda` below. |

Auth column: **—** none, **token** app token required (401 without a valid one).

---

## 1. Accounts, sign-in and MCP tokens

`User = {id, email, display_name, picture_url, created_at}`. `display_name` falls
back to the part of the e-mail before `@`; `picture_url` is Google's profile
picture or `null`.

| Method | Path | Auth | Request | Response | Errors |
|---|---|---|---|---|---|
| POST | `/v1/auth/google` | — | `{id_token}`: the Google ID token (JWT) from Google sign-in | `{app_token, user: User}`. Verifies signature (Google's JWKS), issuer, audience (`GOOGLE_OAUTH_CLIENT_ID`) and expiry; creates the user on first sign-in (keyed by Google `sub`), refreshes e-mail, name and picture after. Each call issues a new app token (one per device). | 401 `invalid Google ID token: <reason>` (expired, issued for another client, bad signature, …); 422; 503 not configured / Google's keys unreachable |
| POST | `/v1/auth/dev` | — | `{email, display_name?}`; header `X-Jolt-Dev-Auth-Secret` when the server has `DEV_AUTH_SECRET` | `{app_token, user: User}`. **Dev only**: mounted when `DEV_AUTH_ENABLED` is on, never in prod. The same e-mail is always the same user (a different user from a Google sign-in with that e-mail). For the front end's dev mode against a local memory-mode server and for `scripts/smoke.py`. | 403 wrong/missing secret; 404 when not enabled; 422 |
| POST | `/v1/auth/sign-out` | token | — | 204; revokes the app token that made the request (other devices stay signed in) | 401 |
| GET | `/v1/me` | token | — | `{user: User, onboarding: {has_track, has_mcp_token, first_sync_at}, mcp_server_url, has_api_key}`. `has_track`: the user owns a track (curated enrolment counts from BE-09). `first_sync_at`: `null` until the first sync run closes, either `finished` or after doing some work (section 2). `mcp_server_url`: the address to paste into Claude / Codex (`MCP_SERVER_URL`, e.g. `https://<host>/mcp/sse`). `has_api_key`: a provider key is stored. | 401 |
| POST | `/v1/mcp-tokens` | token | `{label}` (1–60 characters after trimming, e.g. `"Claude Desktop"`) | **201** `{id, label, last4, created_at, last_used_at: null, token}`. The plaintext `token` appears only in this response. | 401, 422 |
| GET | `/v1/mcp-tokens` | token | — | `[{id, label, last4, created_at, last_used_at}]`, newest first; never the token. `last_used_at` is updated at most once a minute. | 401 |
| DELETE | `/v1/mcp-tokens/{token_id}` | token | — | 204; the next MCP call with that token fails | 401, 404 `token not found` (unknown, another user's, or an app token's id) |

Only hashes are stored: each token document holds an Argon2 hash of the peppered
token and an indexed lookup key (truncated HMAC-SHA256 keyed by the pepper), so
resolving a token is one indexed query plus one Argon2 verify.

---

## 2. Nightly sync

### The loop (MCP, used by the scheduled task)

The scheduled task (prompt: [`NIGHTLY_SYNC_PROMPT.md`](./NIGHTLY_SYNC_PROMPT.md),
daily at 00:15 local, decision D5) calls `jolt_sync`, follows the steps of the
plan it returns, and calls `jolt_sync` again until the plan says `done`. A plan
with source work lists: `jolt_get_unprocessed_sources` → per source
`jolt_get_source_content` → (extract on the agent's compute) →
`jolt_store_extraction` → `jolt_log_session` → `jolt_create_questions`; with
re-reads: `jolt_get_source_content` → `jolt_diff_extractions` →
`jolt_store_extraction(supersede)`; with answers to grade:
`jolt_get_pending_gradings` → `jolt_submit_gradings`; and always a final
`jolt_sync`. FSRS is recomputed inline on grade submission; expired leases are
reclaimed lazily by the claim query (no background job).

**Sync runs.** `jolt_sync` opens a run for the calling MCP token (or continues
its running one). Every sync tool called with that token adds to it: sources
processed (`jolt_store_extraction`), questions written, gradings applied,
`changes` (a `concept_added` per concept from `jolt_log_session`, a
`concept_updated` per `jolt_correct_concept`), and the message of any exception a
tool raises (`"<tool>: <message>"`). The `jolt_sync` whose plan is `done` closes
the run as `finished`; calling it again soon after with still nothing to do does
not open another run. A run with no tool call for `SYNC_RUN_IDLE_SECONDS` (1800)
is closed lazily (by the next `jolt_sync` or status read) as `incomplete`, with
`finished_at` = its last activity. Tool calls up to that long after a run
finished still count towards it. Tools called without a run (no `jolt_sync`
first) record nothing. Runs are per token, so a reconnect continues the same run
and two clients syncing at once get one run each. `users.first_sync_at` is set
when the first run closes `finished`, or `incomplete` after doing some work.

**Sessions (decision D6).** `jolt_log_session` writes one session per upload date
(UTC date of each concept's source): a concept goes to its `source_id`'s date, a
concept without a known source to the date of the first known source in
`source_ids`. Inside a sync run, a later call for a date already logged in that
run extends that date's session.

| Tool | Arguments | Returns | Notes / errors |
|---|---|---|---|
| `jolt_sync` | `limit?=50`, `errors?: [str]` | `SyncPlan` (below) | Opens / continues / closes the run as above; `errors` (problems the model could not resolve) are added to the run. Claims nothing. |
| `jolt_get_unprocessed_sources` | `limit?=10` | `{sources: [{source_id, filename, content_type, lease_expires_at, uploaded_at}]}` | Claims each source (`in_flight`, lease of `LEASE_TTL_SECONDS`, 900 s by default) and notes it on the run. Etag-guarded: a concurrent claimer gets fewer rows. |
| `jolt_get_source_content` | `source_id` | `{source_id, filename, content_type, read_url}` | `read_url` is a signed GET for `BLOB_SAS_TTL_SECONDS`. Raises if the source is not the caller's. |
| `jolt_store_extraction` | `source_id, markdown, confidence?=0, model_id?, supersede?=false` | `{extraction_id, status: "active"}` | Marks the source `processed` and points it at the new extraction. With `supersede`, the previous active extraction becomes `superseded`. Concept reconciliation on supersede is **not implemented yet** (see GAPS). Raises if the source is not the caller's. |
| `jolt_log_session` | `source_ids: [str]`, `concepts: [{text, track_id, title?, concept_key?, syllabus_ref?, parent?, source_id?}]` | `{session_id, session_ids, concept_ids}` | `session_ids`: one per upload date, oldest first (D6); `session_id` is the first. `concept_ids` in input order. Creates concepts (keeping `title`) plus an FSRS card due now, and auto-adds unknown `concept_key`s to each track's agenda (skipped on locked agendas). Not idempotent. |
| `jolt_create_questions` | `concept_id`, `questions: [{stem, options: [str], correct_index, expected_answer}]` | `{question_ids}` (input order) | Not idempotent. The prompt and plan ask for exactly 4 options; the server accepts any number. |
| `jolt_get_pending_gradings` | `limit?=10` | `{gradings: [{review_id, concept_id, question_id, free_text, expected_answer}]}` | Reviews with stage 1 submitted and no final grade. |
| `jolt_submit_gradings` | `gradings: [{review_id, suggested_fsrs_grade: 1–4, rationale?, rubric_version?}]` | `{applied, submitted}` | Idempotent per review. Re-folds each affected card inline. Raises on a grade outside 1–4. |

`SyncPlan`:

| Field | |
|---|---|
| `run_id` | The sync run this call belongs to. |
| `done` | Nothing left for this client: the run is closed, stop. |
| `unprocessed_sources`, `pending_gradings`, `rereads` | Counts waiting now (claimable sources; answers to grade; sources the learner asked to read again). |
| `claimed_sources` | `[{source_id, filename, track_id, uploaded_at, lease_expires_at}]`: sources this run claimed and has not stored yet (they keep the run open). |
| `in_flight_elsewhere` | Sources another client holds under a live lease (they do not keep this run open). |
| `unassigned_sources` | Waiting sources with no track (all of them until BE-05 adds `track_id`); every own track is then listed in `tracks` to choose from. |
| `upload_dates` | Sorted `YYYY-MM-DD` (UTC) upload days of the waiting sources. |
| `tracks` | `[{track_id, name, agenda_status, unprocessed_sources, pending_gradings, rereads, guidance}]` for every track involved; `guidance` says what to do with the agenda for that status. |
| `reread_sources` | `[{source_id, filename, track_id, preferred_model, requested_at}]`. Fed by `SourcesRepository.list_reread_requested` once BE-05 adds it; empty until then. |
| `steps` | Ordered `[{step, tool, purpose, limit, repeat}]`: `limit` is the most items per call (null: one item per call), `repeat` how often (`per batch`, `per claimed source`, `per source or upload date`, `per concept`, `per reread source`, `once, after the steps above`). Empty when done. |
| `limits` | `{sources_per_claim: 5, gradings_per_claim: 20, gradings_per_submit: 20, concepts_per_session: 20, questions_per_concept_min: 1, questions_per_concept_max: 3, options_per_question: 4, lease_seconds}`. |
| `next` | One-line summary of what to do now. |

### Status, runs and prompt (REST, used by the app)

`SyncRun = {id, started_at, finished_at, client_label, status: running|finished|incomplete, sources_processed, questions_written, gradings_submitted, errors: [str], changes: [SyncChange]}`;
`SyncChange = {kind: concept_added|concept_updated|question_rewritten, concept_id, concept_title, track_id, source_id, source_uploaded_at, added_to_syllabus}`.
`client_label` is the MCP token's label. `concept_title` is the concept's
`title`, else its text (cut at 80 characters). `question_rewritten` is reserved
for question versions (BE-06). Up to 500 changes and 50 errors are kept per run.

| Method | Path | Auth | Request | Response | Errors |
|---|---|---|---|---|---|
| GET | `/v1/sync/status` | token | query `tz?` (IANA, e.g. `Europe/London`; default UTC) | `{last_run: SyncRun\|null, next_expected_at, health: ok\|late\|never}`. `next_expected_at`: the next `SYNC_SCHEDULE_TIME` (00:15) in `tz`, in UTC. `health`: `never` without any run, `late` when the last run started more than `SYNC_LATE_AFTER_HOURS` (30) ago, else `ok`. | 401; 422 `unknown time zone: …` |
| GET | `/v1/sync/runs` | token | query `limit?=10` (1–100) | `[SyncRun]`, newest first | 401, 422 |
| GET | `/v1/sync/prompt` | token | — | `{prompt, schedule_time: "00:15", version}`: the scheduled-task prompt (markdown) from `docs/NIGHTLY_SYNC_PROMPT.md` | 401 |

## 3. Sources and uploads

The app (REST) or an agent (MCP) asks for a signed PUT URL, the client uploads the
bytes straight to Blob, then the app confirms. The source stays `unprocessed` until
a sync claims it.

The PUT to `upload_url` is a plain Azure Blob "Put Blob" with a SAS: send the raw
bytes with headers `x-ms-blob-type: BlockBlob` and `Content-Type: <content_type>`;
success is `201`. Browsers can do this from the origins in Blob CORS (see
`docs/DEPLOY.md`).

| Method | Path | Auth | Request | Response | Errors |
|---|---|---|---|---|---|
| POST | `/v1/uploads/request` | token | `{filename, content_type, sha256}` | `{source_id, upload_url}`; PUT the bytes to `upload_url` (expires after `BLOB_SAS_TTL_SECONDS`) | 401, 422 |
| POST | `/v1/uploads/{source_id}/confirm` | token | `{sha256?}` | `{status: "ready"}` | 400 `source not found` / `blob not found — upload did not complete` / `sha256 mismatch…`; 401; 422 |
| GET | `/v1/sources` | token | — | `[{id, filename, content_type, processing_status: unprocessed\|in_flight\|processed, active_extraction_id, created_at}]`, newest first | 401 |
| GET | `/v1/sources/{source_id}/extractions` | token | — | `[{id, source_id, confidence, model_id, status: active\|superseded, created_at}]`, newest first. No markdown. | 401, 404 `source not found` |

| Tool | Arguments | Returns | Notes / errors |
|---|---|---|---|
| `jolt_request_upload_url` | `filename, content_type, sha256` | `{source_id, upload_url}` | Same as `POST /v1/uploads/request`; confirm is REST-only. |
| `jolt_list_sources` | — | `{sources: [{source_id, filename, processing_status, active_extraction_id}]}` | Read-only. |
| `jolt_diff_extractions` | `track_id`, `proposed_concepts: [{text, title?, syllabus_ref?}]` | `{deltas: [{change: unchanged\|refined\|changed\|removed\|new, existing_concept_id, proposed_text, similarity}]}` | Read-only preview against the track's active concepts. |

---

## 4. Review

Two stages. Stage 1 stores the learner's own words and only then releases the
options; stage 2 scores the choice, sets a provisional grade and recomputes FSRS
inline. The agent later replaces the provisional grade (section 2).

| Method | Path | Auth | Request | Response | Errors |
|---|---|---|---|---|---|
| GET | `/v1/reviews/due` | token | query `limit?=100` (cards) | `{count, items: [{concept_id, question_id, stem}]}`: every active question of each due card, most overdue card first | 401, 422 |
| POST | `/v1/reviews/{question_id}/stage1` | token | `{concept_id, free_text}` | `{review_id, options: [str]}` | 401, 404 `question not found`, 422 |
| POST | `/v1/reviews/{review_id}/stage2` | token | `{selected_index, latency_ms?}` | `{correct, expected_answer, correct_index, due_at}` | 400 `review not found` / `stage 1 not submitted…` / `stage 2 already submitted` / `question not found`; 401; 422 |

---

## 5. Tracks, agenda and progress

| Method | Path | Auth | Request | Response | Errors |
|---|---|---|---|---|---|
| GET | `/v1/tracks` | token | — | `[Track]`: the caller's tracks (newest first), then curated ones. `Track = {id, name, origin: user\|jolt, syllabus: [label], agenda: Agenda, user_id}` | 401 |
| POST | `/v1/tracks` | token | `{name, syllabus?: [str]}` | `Track`. A non-empty syllabus gives a `refined` agenda with source `user` (each label is also its `concept_key`); none gives `none`. | 401, 422 |
| POST | `/v1/tracks/{track_id}/agenda/lock` | token | — | `Agenda` (status `locked`) | 401, 404 (not found, or not the caller's track) |
| POST | `/v1/tracks/{track_id}/agenda/unlock` | token | — | `Agenda` (`refined` if it has a syllabus, else `none`) | 401, 404 |
| GET | `/v1/tracks/{track_id}/coverage` | token | — | `{track_id, total_concepts, studied_concepts, decaying_concepts, coverage_ratio, by_syllabus: [{syllabus_ref, total_concepts, studied_concepts, coverage_ratio}], uncovered_syllabus: [str]}` | 401, 404 |
| GET | `/v1/progress/concepts/{concept_id}` | token | — | `{concept_id, stability, difficulty, retrievability, due_at, reps, lapses, timeline: [{reviewed_at, grade, is_correct}]}` | 401, 404 `no state for this concept` |

| Tool | Arguments | Returns | Notes / errors |
|---|---|---|---|
| `jolt_get_tracks` | — | `{tracks: [{id, name, origin, syllabus, agenda}]}` | Read-only. |
| `jolt_get_agenda` | `track_id` | `Agenda` | `{error}` on: `track not found`. |
| `jolt_set_agenda` | `track_id`, `syllabus: [{concept_key, label, parent?}]`, `source?=agent`, `mark_refined?=false` | `Agenda` | Replaces the whole syllabus. Etag-serialised with auto-add. `{error}` on: locked agenda, invalid `source`, item missing `concept_key`/`label`, `track not found`. |
| `jolt_get_recent_concepts` | `track_id, limit?=20` | `{concepts: [{id, text, syllabus_ref}]}` | Newest first. |
| `jolt_get_coverage` | `track_id` | `{track_id, total_concepts, studied_concepts, decaying_concepts, coverage_ratio, uncovered_syllabus}` | Raises `track not found`. |
| `jolt_correct_concept` | `track_id, concept_id, text` | `{concept_id, updated}` | `updated: false` when the write lost a concurrency race. `{error}` on: `concept not found`. |

---

## 6. Settings

| Method | Path | Auth | Request | Response | Errors |
|---|---|---|---|---|---|
| POST | `/v1/settings/api-key` | token | `{provider: anthropic\|openai, key}` | `{provider, last4, status: "validated"}`. The key is checked with the provider, stored in Key Vault, and only a reference is kept in Cosmos. | 400 unsupported provider; 401; 422 provider rejected the key (string `detail`) or invalid body |
| GET | `/v1/settings/api-key` | token | — | `[{provider, last4, status: "stored"}]` | 401 |
| DELETE | `/v1/settings/api-key/{provider}` | token | — | 204 (Key Vault secret first, then the reference) | 400 unsupported provider; 401 |
| PUT | `/v1/settings/retention` | token | `{retention_days?: int\|null}` | `{retention_days}` | 401, 422 |

---

## Testing against the API without Azure

`STORAGE_MODE=memory DEV_AUTH_ENABLED=true uvicorn jolt.main:app` serves this
whole API on in-memory fakes (`jolt/data/memory.py`); sign in with
`POST /v1/auth/dev {"email": "you@example.com"}`, then create an MCP token for
agents. Tests use the same fakes (`tests/conftest.py`) and verify Google ID tokens
minted with a test RSA key, so they never call Google.

Uploads work too when the server knows its own URL:

```bash
STORAGE_MODE=memory DEV_AUTH_ENABLED=true PUBLIC_BASE_URL=http://localhost:8000 uvicorn jolt.main:app --port 8000
```

`upload_url` / `read_url` then point at `/_dev/blob/<container>/<path>?sp=…&sig=…`
on that server, which accepts the same PUT (201) and GET as Blob, checks the
signature, and is mounted only in memory mode (not part of the contract). Without
`PUBLIC_BASE_URL` the URLs are opaque `memory://` strings that nothing serves.
`python scripts/smoke.py --base-url http://localhost:8000` runs the whole loop.

`GET /health` returns `{status: "ok", version}`; `version` is the image's git SHA
(`dev` locally).
