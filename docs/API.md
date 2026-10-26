# Jolt API reference

The human-readable view of the contract. The machine-readable source of truth is
generated and checked in CI:

- [`contract/openapi.json`](../contract/openapi.json) — REST, every `/v1` route.
- [`contract/mcp-tools.json`](../contract/mcp-tools.json) — every MCP tool: name,
  full agent-facing description, input schema, output schema.

Regenerate both with `python scripts/export_contract.py` after changing a route, a
tool, a tool description or `jolt/mcp/schemas.py`. This file is written by hand:
update it in the same change.

State: API version 0.2.0, the MVP plus the track agenda model (PR #3). Screens that
have no endpoint yet are listed in [`GAPS.md`](./GAPS.md).

## Conventions

| | |
|---|---|
| **Base paths** | REST under `/v1`. MCP over HTTP+SSE: connect to `/mcp/sse`, post to `/mcp/messages/`. `GET /health` → `{status: "ok", version}` (no auth). CORS: the origins in `CORS_ORIGINS` / `CORS_ORIGIN_REGEX`, no credentials (the token is a header). |
| **Auth** | `Authorization: Bearer <token>` on every `/v1` route except `POST /v1/accounts`, and on the MCP connection. Today there is one token kind (`jolt_live_…`) for both surfaces; BE-03 splits app and MCP tokens. A bare token without `Bearer` is also accepted. |
| **Scope** | Identity comes only from the token. No route or tool takes a `user_id`. |
| **Errors (REST)** | Non-2xx bodies are `{"detail": "<message>"}` (`ErrorResponse`). Request-validation failures are `422` with `{"detail": [ {loc, msg, type} ]}` (`HTTPValidationError`). `401` always carries `WWW-Authenticate: Bearer`. |
| **Errors (MCP)** | Tools marked *`{error}` on* return `{"error": "<message>"}` for the listed recoverable cases. Every other failure (missing/invalid token, unknown source, grade outside 1–4) is raised and arrives as an MCP tool error (`isError: true`). |
| **Types** | IDs are opaque strings (32-hex today). Timestamps are ISO-8601 UTC. FSRS grades: 1 Again, 2 Hard, 3 Good, 4 Easy. |
| **Agenda** | `{status: none\|draft\|refined\|locked, source: agent\|user\|emergent, syllabus: [{concept_key, label, parent}], last_refined_at, refined_from_source_id}`; written `Agenda` below. |

Auth column: **—** none, **token** bearer token required (401 without a valid one).

---

## 1. Accounts

| Method | Path | Auth | Request | Response | Errors |
|---|---|---|---|---|---|
| POST | `/v1/accounts` | — | `{display_name?: string}` | `{user_id, token}`; the token appears only in this response and cannot be read back | 422 |

There is no sign-in, `/me` or token management yet (BE-03).

---

## 2. Sync (MCP, used by the nightly agent)

The agent's loop: `jolt_sync` → `jolt_get_unprocessed_sources` → per source
`jolt_get_source_content` → (extract on the agent's compute) →
`jolt_store_extraction` → `jolt_log_session` → `jolt_create_questions`; then
`jolt_get_pending_gradings` → `jolt_submit_gradings`. FSRS is recomputed inline on
grade submission; expired leases are reclaimed lazily by the claim query (no
background job).

| Tool | Arguments | Returns | Notes / errors |
|---|---|---|---|
| `jolt_sync` | `limit?=50` | `{unprocessed_sources, pending_gradings}` (counts) | Read-only. Counts only claimable sources. |
| `jolt_get_unprocessed_sources` | `limit?=10` | `{sources: [{source_id, filename, content_type, lease_expires_at}]}` | Claims each source (`in_flight`, lease of `LEASE_TTL_SECONDS`, 900 s by default). Etag-guarded: a concurrent claimer gets fewer rows. |
| `jolt_get_source_content` | `source_id` | `{source_id, filename, content_type, read_url}` | `read_url` is a signed GET for `BLOB_SAS_TTL_SECONDS`. Raises if the source is not the caller's. |
| `jolt_store_extraction` | `source_id, markdown, confidence?=0, model_id?, supersede?=false` | `{extraction_id, status: "active"}` | Marks the source `processed` and points it at the new extraction. With `supersede`, the previous active extraction becomes `superseded`. Concept reconciliation on supersede is **not implemented yet** (see GAPS). Raises if the source is not the caller's. |
| `jolt_log_session` | `source_ids: [str]`, `concepts: [{text, track_id, title?, concept_key?, syllabus_ref?, parent?, source_id?}]` | `{session_id, concept_ids}` (input order) | Creates concepts plus an FSRS card due now, and auto-adds unknown `concept_key`s to each track's agenda (skipped on locked agendas). Not idempotent. |
| `jolt_create_questions` | `concept_id`, `questions: [{stem, options: [str], correct_index, expected_answer}]` | `{question_ids}` (input order) | Not idempotent. |
| `jolt_get_pending_gradings` | `limit?=10` | `{gradings: [{review_id, concept_id, question_id, free_text, expected_answer}]}` | Reviews with stage 1 submitted and no final grade. |
| `jolt_submit_gradings` | `gradings: [{review_id, suggested_fsrs_grade: 1–4, rationale?, rubric_version?}]` | `{applied, submitted}` | Idempotent per review. Re-folds each affected card inline. Raises on a grade outside 1–4. |

---

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

`STORAGE_MODE=memory uvicorn jolt.main:app` serves this whole API on in-memory
fakes (`jolt/data/memory.py`). Tests use the same fakes (`tests/conftest.py`).

Uploads work too when the server knows its own URL:

```bash
STORAGE_MODE=memory PUBLIC_BASE_URL=http://localhost:8000 uvicorn jolt.main:app --port 8000
```

`upload_url` / `read_url` then point at `/_dev/blob/<container>/<path>?sp=…&sig=…`
on that server, which accepts the same PUT (201) and GET as Blob, checks the
signature, and is mounted only in memory mode (not part of the contract). Without
`PUBLIC_BASE_URL` the URLs are opaque `memory://` strings that nothing serves.
`python scripts/smoke.py --base-url http://localhost:8000` runs the whole loop.

`GET /health` returns `{status: "ok", version}`; `version` is the image's git SHA
(`dev` locally).
