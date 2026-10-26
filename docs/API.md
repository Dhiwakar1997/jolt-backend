# Jolt API reference

The human-readable view of the contract. The machine-readable source of truth is
generated and checked in CI:

- [`contract/openapi.json`](../contract/openapi.json) — REST, every `/v1` route.
- [`contract/mcp-tools.json`](../contract/mcp-tools.json) — every MCP tool: name,
  full agent-facing description, input schema, output schema.

Regenerate both with `python scripts/export_contract.py` after changing a route, a
tool, a tool description or `jolt/mcp/schemas.py`. This file is written by hand:
update it in the same change.

State: API version 0.8.0: the MVP, the track agenda model (PR #3), accounts,
Google sign-in and MCP token management (BE-03), the nightly sync: the
`jolt_sync` plan, sync runs, sync status and the scheduled-task prompt (BE-04),
sources and uploads: multi-page uploads, source list and detail with past
reads, read again and storage (BE-05), the review loop: review sessions,
idempotent stages, question versions and retiring mastered concepts (BE-06),
and tracks and concepts: the Tracks, Track detail and Concept read models,
corrections, off-plan concepts, ownership checks and supersede reconciliation
(BE-07), and curated tracks: the catalog, enrolment, the DSA pattern trainer
imported from v1 (pattern library, decision tree, `pattern_match` questions) and
DSA sessions drawn by the v1 rules (BE-09, section 7). Screens that have no
endpoint yet are listed in [`GAPS.md`](./GAPS.md).

## Conventions

| | |
|---|---|
| **Base paths** | REST under `/v1`. MCP over HTTP+SSE: connect to `/mcp/sse`, post to `/mcp/messages/`. `GET /health` → `{status: "ok", version}` (no auth). CORS: the origins in `CORS_ORIGINS` / `CORS_ORIGIN_REGEX`, no credentials (the token is a header). |
| **Auth** | `Authorization: Bearer <token>`. Two opaque token kinds: an **app token** (`jolt_app_…`, one per signed-in device, from sign-in) on every `/v1` route except the sign-in routes, and an **MCP token** (`jolt_live_…`, one per connected client, from `POST /v1/mcp-tokens`) on the MCP connection. Each surface refuses the other kind with `401` (`detail: "wrong token kind: …"`); opening `/mcp/sse` with a missing, revoked or wrong-kind token is a `401` before the stream starts. On `/mcp/sse` only, the MCP token may instead ride the URL as `?token=` (for connectors that take only a URL; `MCP_QUERY_TOKEN_ENABLED`, redacted from access logs); client setup in [`CONNECT.md`](./CONNECT.md). Tokens do not expire; revoking (sign-out, `DELETE /v1/mcp-tokens/{id}`) takes effect on the next request. A bare token without `Bearer` is also accepted. |
| **Scope** | Identity comes only from the token. No route or tool takes a `user_id`. |
| **Ownership** (BE-07, A16, BE-09) | A **track** is *visible* when it is the caller's or curated, *owned* only when it is the caller's: reads take a visible track, writes (agenda, rename, archive, logging concepts) an owned one, so curated tracks are read-only. A **concept** is the caller's when they hold its card (FSRS state). On a curated track the concepts are Jolt's (`curated`), readable by everyone and written by no client: no questions, text changes or correction notes, even once the learner holds a card for one. A **question** is the caller's when its concept is; a curated `pattern_match` question is answerable by learners enrolled in its track (their card is created by the first answer). Anything else answers exactly like an unknown id (404 / the tool's not-found). |
| **Errors (REST)** | Non-2xx bodies are `{"detail": "<message>"}` (`ErrorResponse`). Request-validation failures are `422` with `{"detail": [ {loc, msg, type} ]}` (`HTTPValidationError`). `401` always carries `WWW-Authenticate: Bearer`. |
| **Errors (MCP)** | Tools marked *`{error}` on* return `{"error": "<message>"}` for the listed recoverable cases. Every other failure (missing/invalid token, unknown source, grade outside 1–4) is raised and arrives as an MCP tool error (`isError: true`). |
| **Types** | IDs are opaque strings (32-hex today). Timestamps are ISO-8601 UTC. FSRS grades: 1 Again, 2 Hard, 3 Good, 4 Easy. |
| **Agenda** | `{status: none\|draft\|refined\|locked, source: agent\|user\|emergent, syllabus: [{concept_key, label, parent, source}], last_refined_at, refined_from_source_id, outline}`; written `Agenda` below. An item's `source` is who added it (`user`: the learner's outline or syllabus, `agent`: a refine, `emergent`: auto-added from notes); items written before BE-07 report the agenda's `source`. `outline` is the learner's raw outline from track creation (else `null`). |

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
| GET | `/v1/me` | token | — | `{user: User, onboarding: {has_track, has_mcp_token, first_sync_at}, mcp_server_url, has_api_key}`. `has_track`: the user owns a track or is enrolled in a curated one (BE-09). `first_sync_at`: `null` until the first sync run closes, either `finished` or after doing some work (section 2). `mcp_server_url`: the address to paste into Claude / Codex (`MCP_SERVER_URL`, e.g. `https://<host>/mcp/sse`). `has_api_key`: a provider key is stored. | 401 |
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
read-again requests (claimed like any source, marked `supersede: true`) a
`jolt_diff_extractions` preview comes before `jolt_store_extraction(supersede,
concepts)`, which reconciles the new read's concepts with the old ones instead of
`jolt_log_session` (reconciliation, below; `jolt_continue_reconciliation` while it
is `partial`); with answers to grade: `jolt_get_pending_gradings` →
`jolt_submit_gradings`; with correction notes waiting (BE-07):
`jolt_get_pending_corrections` → `jolt_correct_concept`; and always a final
`jolt_sync`. The prompt also asks to tidy any `draft` agenda with an `outline`
(`jolt_set_agenda`, `mark_refined`). FSRS is recomputed inline on grade
submission; expired leases are reclaimed lazily by the claim query (no
background job).

**Sync runs.** `jolt_sync` opens a run for the calling MCP token (or continues
its running one). Every sync tool called with that token adds to it: sources
processed (`jolt_store_extraction`), questions written, gradings applied,
`changes` (a `concept_added` per concept from `jolt_log_session` and per new or
successor concept a reconciliation applies, a `concept_updated` per refined
concept and per `jolt_correct_concept` that changed the text), and the message of any exception a
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
(UTC date of each concept's source's `uploaded_at`, the confirm time): a concept goes to its `source_id`'s date, a
concept without a known source to the date of the first known source in
`source_ids`. Inside a sync run, a later call for a date already logged in that
run extends that date's session.

| Tool | Arguments | Returns | Notes / errors |
|---|---|---|---|
| `jolt_sync` | `limit?=50`, `errors?: [str]` | `SyncPlan` (below) | Opens / continues / closes the run as above; `errors` (problems the model could not resolve) are added to the run. Claims nothing. Counts only claimable, confirmed sources; read-again requests are listed in `reread_sources` (and counted as source work); concepts with correction notes waiting are counted in `pending_corrections` and keep the plan open (BE-07). |
| `jolt_get_unprocessed_sources` | `limit?=10` | `{sources: [{source_id, filename, content_type, lease_expires_at, uploaded_at, title, track_id, page_count, supersede, preferred_model}]}` | Claims each source (`in_flight`, lease of `LEASE_TTL_SECONDS`, 900 s by default) and notes it on the run. Etag-guarded: a concurrent claimer gets fewer rows. Only confirmed uploads. `uploaded_at`'s date decides the source's session (D6). `supersede: true` = a read-again request on a source that already has a read; `preferred_model` is the learner's pick for it. A source whose lease lapses `SOURCE_MAX_ATTEMPTS` (3) times is marked `failed` instead of claimed. |
| `jolt_get_source_content` | `source_id` | `{source_id, filename, content_type, read_url, page_count, pages: [{index, filename, content_type, read_url}]}` | One signed GET per page, valid `BLOB_SAS_TTL_SECONDS`; the top-level fields are page 0's. Raises if the source is not the caller's. |
| `jolt_store_extraction` | `source_id, markdown, confidence?=0, model_id?, supersede?=false, concepts?: [{text, track_id?, title?, concept_key?, syllabus_ref?, parent?, page?}]` | `{extraction_id, status: "active", reconciliation: Reconciliation \| null}` *`{error}` on* | Marks the source `processed` (or back to `unprocessed` if a read-again request arrived while the sync held it) and points it at the new extraction; a read-again request made before the claim is answered (cleared). The previous active extraction always becomes `superseded` (one active read per source). Records the MCP token's label as the read's `client_label` and the source on the run. With `concepts`, reconciles them against the replaced read's concepts (below); `track_id` defaults to the source's track. `{error}` (nothing stored) on: a concept with no text, or no `track_id` when the source has no track. Raises if the source, or a concept's track, is not the caller's. |
| `jolt_continue_reconciliation` | `source_id` | `Reconciliation` *`{error}` on* | Resumes a `partial` reconciliation of the source's active read from its checkpoint (up to the ceiling again); the steps it applies are added to the run's changes. A `complete` one is returned unchanged. `{error}` when the active read has none (stored without `concepts`). Raises if the source is not the caller's. |
| `jolt_log_session` | `source_ids: [str]`, `concepts: [{text, track_id, title?, concept_key?, syllabus_ref?, parent?, source_id?, page?}]` | `{session_id, session_ids, concept_ids}` | `session_ids`: one per upload date, oldest first (D6); `session_id` is the first. `concept_ids` in input order. Creates concepts (keeping `title`) plus an FSRS card due now, and auto-adds unknown `concept_key`s to each track's agenda (skipped on locked agendas). `page` is the 1-based source page (Concept → "where found"). A concept with a `source_id` is credited to that source's active read (`concepts_found` in past reads) and recorded as a `concept_added` change on the run. Raises, writing nothing, if a `track_id` is not one of the caller's own tracks (curated ones are refused) or a source id (in `source_ids` or a concept's `source_id`) is not theirs (BE-07). Not idempotent. |
| `jolt_create_questions` | `concept_id`, `questions: [{stem, options: [str], correct_index, expected_answer}]` | `{question_ids}` (input order) | Raises if the concept is not the caller's (BE-07) or is on a curated track (read-only, BE-09). Not idempotent. Adds to `questions_written` on the read the concept came from and on the run, and refreshes the card's active questions (Track detail's question count and version). The prompt and plan ask for exactly 4 options; the server accepts any number. |
| `jolt_get_pending_gradings` | `limit?=10` | `{gradings: [{review_id, concept_id, question_id, free_text, expected_answer, stem, options, correct_index, question_version, selected_index, is_correct}], allow_question_rewrite}` | Reviews with stage 1 submitted and no final grade. The question fields are the version the learner answered (BE-06), so the agent can rewrite it; `allow_question_rewrite` is the user's opt-in (decision D8). |
| `jolt_get_pending_corrections` | — | `{corrections: [{concept_id, track_id, title, text, notes: [{note, created_at}]}]}` | The learner's notes from `POST /v1/concepts/{id}/correction`, oldest request first (BE-07). Answer each with `jolt_correct_concept`. Read-only. |
| `jolt_correct_concept` | `track_id, concept_id, text` | `{concept_id, updated, corrections_cleared}` *`{error}` on* | Replaces the caller's concept text in place (questions, reviews and card kept) and clears its correction notes; sending the current text re-confirms it (notes cleared, no change recorded). A changed text is a `concept_updated` change on the run. `updated: false` when the write lost a race. `{error: "concept not found"}` when the concept is not the caller's, not on `track_id`, or curated (BE-09). |
| `jolt_submit_gradings` | `gradings: [{review_id, suggested_fsrs_grade: 1–4, rationale?, rubric_version?, rewritten_question?: {stem, options, correct_index, expected_answer, rewrite_reason?}}]` | `{applied, submitted, rewritten: [{review_id, question_id, version, previous_version_id}], rewrites_skipped: [{review_id, reason}]}` | Idempotent per review. Re-folds each affected card inline (FSRS and mastery). `rationale` is the feedback the learner sees. A `rewritten_question` becomes version n+1 of the question only when the user's `allow_question_rewrite` is on (section 4, question versions); otherwise, or when invalid, it is listed in `rewrites_skipped` and the grade still lands. A retry lists the version it created again, never a second one. Each grade applied adds to `gradings_submitted` on the run, and each new version is a `question_rewritten` change on it and becomes the card's current question (`active_question_ids`, `question_version`; BE-07). Raises on a grade outside 1–4. |

`SyncPlan`:

| Field | |
|---|---|
| `run_id` | The sync run this call belongs to. |
| `done` | Nothing left for this client (no source work, answers to grade or correction notes): the run is closed, stop. |
| `unprocessed_sources`, `pending_gradings`, `rereads` | Counts waiting now (claimable sources; answers to grade; how many waiting sources are read-again requests, `len(reread_sources)`). |
| `pending_corrections` | Concepts with learner correction notes waiting (BE-07), the ones `jolt_get_pending_corrections` returns. When non-zero the plan adds `jolt_get_pending_corrections` (once) → `jolt_correct_concept` (per flagged concept). |
| `claimed_sources` | `[{source_id, filename, track_id, uploaded_at, lease_expires_at}]`: sources this run claimed and has not stored yet (they keep the run open). |
| `in_flight_elsewhere` | Sources another client holds under a live lease (they do not keep this run open). |
| `unassigned_sources` | Waiting sources with no track (uploaded without `track_id`, e.g. by an agent); every own track is then listed in `tracks` to choose from. |
| `upload_dates` | Sorted `YYYY-MM-DD` (UTC) upload days of the waiting sources. |
| `tracks` | `[{track_id, name, agenda_status, unprocessed_sources, pending_gradings, rereads, pending_corrections, guidance}]` for every track involved; `guidance` says what to do with the agenda for that status. |
| `reread_sources` | `[{source_id, title, filename, track_id, preferred_model, requested_at}]`: the read-again requests (`POST /v1/sources/{id}/reread`) among the claimable sources and the ones this run holds. They are counted in `unprocessed_sources` / `claimed_sources` too; the claim hands each out with `supersede: true`. `preferred_model` (`claude`, `codex`, or `null`) is a hint, not a lock. When there are any, the plan adds a `jolt_diff_extractions` step before `jolt_store_extraction`, whose purpose asks for the read's `concepts` (reconciliation). |
| `steps` | Ordered `[{step, tool, purpose, limit, repeat}]`: `limit` is the most items per call (null: one item per call), `repeat` how often (`per batch`, `per claimed source`, `per source or upload date`, `per concept`, `per reread source`, `once`, `per flagged concept`, `once, after the steps above`). Empty when done. |
| `limits` | `{sources_per_claim: 5, gradings_per_claim: 20, gradings_per_submit: 20, concepts_per_session: 20, questions_per_concept_min: 1, questions_per_concept_max: 3, options_per_question: 4, lease_seconds}`. |
| `next` | One-line summary of what to do now. |

**Reconciliation (BE-07 §6).** `Reconciliation = {status: complete|partial,
total_steps, applied_steps, remaining_steps, max_steps_per_call, concept_ids
(input order), deltas: [{index (input position, null for removed), change,
concept_id, existing_concept_id, similarity, applied}]}`. The proposed concepts
are classified per track against the concepts the replaced read produced
(`jolt_diff_extractions` previews the same classifier):

| change | effect |
|---|---|
| `unchanged` | kept as is, credited to the new read (`is_new: false`) |
| `refined` | text (and title, key, page) updated in place; questions, reviews and card kept |
| `changed` | a successor concept is created whose card **inherits the old card's FSRS state** (its later folds start from it); the old concept becomes `superseded` (`successor_id`), its questions are retired and its card leaves the queue; its reviews stay on it as history. Write questions for the successor. |
| `removed` | the old concept becomes `superseded` with no successor: out of the queue and off Track detail, kept for history (still readable at `GET /v1/concepts/{id}`) |
| `new` | a new concept with a card due now (`is_new: true`); write questions for it |

New keys are auto-added to the agenda, as `jolt_log_session` does.
**Ceiling:** one call applies at most `RECONCILE_MAX_CONCEPTS` steps (default
**25**; one step per proposed or removed concept). Cosmos has no transaction
across the partitions involved (concepts by track, cards by user, questions by
concept), so the plan is stored on the new read as a checkpoint and the cursor
is saved after each step; a larger supersede answers `partial` and the agent
calls `jolt_continue_reconciliation` until `complete`. New and successor concepts
and their cards have ids fixed in the plan, and every write in a step converges
when repeated, so a call that dies mid-step is repaired by resuming. A concept
not applied yet does not exist (questions for it are refused). Up to 500
concepts per read. Without `concepts`, a supersede only moves the pointer and
the old concepts stay active.

### Status, runs and prompt (REST, used by the app)

`SyncRun = {id, started_at, finished_at, client_label, status: running|finished|incomplete, sources_processed, questions_written, gradings_submitted, errors: [str], changes: [SyncChange]}`;
`SyncChange = {kind: concept_added|concept_updated|question_rewritten, concept_id, concept_title, track_id, source_id, source_uploaded_at, added_to_syllabus}`.
`client_label` is the MCP token's label. `concept_title` is the concept's
`title`, else its text (cut at 80 characters). `question_rewritten`: a
`jolt_submit_gradings` rewrite created a new question version for the concept
(BE-06). `concept_updated`: `jolt_correct_concept` changed the text, or a
reconciliation refined the concept (BE-07). Up to 500 changes and 50 errors are
kept per run.

| Method | Path | Auth | Request | Response | Errors |
|---|---|---|---|---|---|
| GET | `/v1/sync/status` | token | query `tz?` (IANA, e.g. `Europe/London`; default UTC) | `{last_run: SyncRun\|null, next_expected_at, health: ok\|late\|never}`. `next_expected_at`: the next `SYNC_SCHEDULE_TIME` (00:15) in `tz`, in UTC. `health`: `never` without any run, `late` when the last run started more than `SYNC_LATE_AFTER_HOURS` (30) ago, else `ok`. | 401; 422 `unknown time zone: …` |
| GET | `/v1/sync/runs` | token | query `limit?=10` (1–100) | `[SyncRun]`, newest first | 401, 422 |
| GET | `/v1/sync/prompt` | token | — | `{prompt, schedule_time: "00:15", version}`: the scheduled-task prompt (markdown) from `docs/NIGHTLY_SYNC_PROMPT.md` | 401 |

---

## 3. Sources and uploads

The app (REST) or an agent (MCP) asks for signed PUT URLs, the client uploads the
bytes straight to Blob, then the app confirms. A note of several pages is **one
source with N pages** (decision A7). The source stays `unprocessed` until a sync
claims it; unconfirmed uploads are invisible to the app and never offered to a
sync.

Each PUT to an `upload_url` is a plain Azure Blob "Put Blob" with a SAS: send the
raw bytes with headers `x-ms-blob-type: BlockBlob` and `Content-Type:
<content_type>`; success is `201`. Browsers can do this from the origins in Blob
CORS (see `docs/DEPLOY.md`).

**Limits.** Accepted types: `image/jpeg`, `image/png`, `image/heic`, `image/webp`,
`application/pdf` (case and `;` parameters ignored). At most `UPLOAD_MAX_BYTES` per
file (25 MiB) and `UPLOAD_MAX_PAGES` files per upload (30). Both are returned by
the request call.

**Confirm checks**, per page: the blob exists; its size is within the cap (an
oversize file is deleted) and equals the declared `size`; the SHA-256 of the
stored bytes equals the declared `sha256` (the server streams each file once to
hash it). If the body carries `pages`, each entry must match the request.
Confirming twice is fine.

**Status**: `unprocessed` → `in_flight` (claimed by a sync, under a lease) →
`processed`; `failed` when the lease lapsed `SOURCE_MAX_ATTEMPTS` (3) times without
an extraction. Read again puts a source back to `unprocessed` (a failed one gets
fresh attempts).

**Low confidence**: a read whose `confidence` is under `LOW_CONFIDENCE_THRESHOLD`
(0.6) has `low_confidence: true`; a source has it when its active read does.

`Read = {id, source_id, number (1-based, oldest first), model_id, client_label
(the MCP token's label), confidence, low_confidence, status: active|superseded,
created_at, concepts_found, questions_written, concepts: [{concept_id, title,
is_new}]}`. Concepts are credited to the source's active read when
`jolt_log_session` names their `source_id`; `questions_written` counts questions
`jolt_create_questions` wrote for those concepts. `is_new` is `true` for a concept
first found by that read and `false` for one a reconciled re-read carried over
(unchanged, refined or changed, BE-07).

`Source = {id, filename, content_type (page 0's), processing_status, active_extraction_id,
created_at, title, track_id, track_name, kind: photos|pdf|file, page_count,
uploaded_at (confirm time), concepts_found, questions_written (of the active
read), low_confidence, reread_requested: {requested_at, preferred_model} | null,
read_count (reads stored, for the "Reads" column)}`.

| Method | Path | Auth | Request | Response | Errors |
|---|---|---|---|---|---|
| POST | `/v1/uploads/request` | token | `{track_id?, title?, pages: [{filename, content_type, size, sha256}]}`, or for one file `{track_id?, title?, filename, content_type, sha256, size?}`. `title` defaults to the first filename. | `{source_id, upload_url (page 0), pages: [{index, upload_url}], max_bytes_per_file, accepted_types}`; URLs expire after `BLOB_SAS_TTL_SECONDS` | 401; 404 `track not found` (not the caller's track); 422 field errors (type, size, hash format, too many files, neither shape) |
| POST | `/v1/uploads/{source_id}/confirm` | token | `{sha256?, pages?: [{index, sha256, size}]}` | `{status: "ready"}` | 400 `source not found` / `blob not found — upload did not complete on page N` / `size mismatch on page N…` / `sha256 mismatch on page N…` / `file on page N is larger than … bytes` / `…between request and confirm`; 401; 422 |
| GET | `/v1/sources` | token | query `track_id?`, `status?` (`unprocessed\|in_flight\|processed\|failed`) | `[Source]`, newest first; confirmed uploads only | 401, 422 |
| GET | `/v1/sources/{source_id}` | token | — | `Source` + `{pages: [{index, url, content_type, size}], active_read: Read + {markdown} \| null, reads: [Read]}`. `url` is a signed GET of the original file (`BLOB_SAS_TTL_SECONDS`); `reads` newest first | 401, 404 `source not found` |
| POST | `/v1/sources/{source_id}/reread` | token | `{preferred_model?: string\|null}` (≤ 64 chars, e.g. `claude`, `codex`) | `{source_id, requested_at, preferred_model}`. The next `jolt_sync` lists it in `reread_sources`; a request made while a sync holds the source survives that sync | 401, 404 |
| GET | `/v1/sources/{source_id}/extractions` | token | — | `[{id, source_id, confidence, model_id, status: active\|superseded, created_at}]`, newest first. No markdown (use the detail route). | 401, 404 `source not found` |
| GET | `/v1/storage` | token | — | `{bytes_used, source_count, page_count}` over the caller's confirmed uploads | 401 |

The Reader gets **originals only**: `pages[].url` points at the uploaded file
(JPEG, PNG, HEIC, WebP or PDF). There are no resized renditions or thumbnails
(see GAPS, Reader).

| Tool | Arguments | Returns | Notes / errors |
|---|---|---|---|
| `jolt_request_upload_url` | `filename, content_type, sha256` | `{source_id, upload_url}` or `{error}` | One page. Same limits as `POST /v1/uploads/request`; `{error}` on an unsupported type or a malformed hash. Confirm is REST-only. |
| `jolt_list_sources` | — | `{sources: [{source_id, filename, processing_status, active_extraction_id, page_count, reread_requested}]}` | Read-only. Confirmed uploads only. |
| `jolt_diff_extractions` | `track_id`, `proposed_concepts: [{text, title?, syllabus_ref?}]` | `{deltas: [{change: unchanged\|refined\|changed\|removed\|new, existing_concept_id, proposed_text, similarity}]}` | Read-only preview against the caller's active concepts on the track. Raises if the track is neither the caller's nor curated. |

---

## 4. Review

A review session serves an ordered queue; each question is answered in two
stages. Stage 1 stores the learner's own words and only then releases the
options (no other route returns options, correct index or expected answer
before it: the server-side stage lock); stage 2 scores the choice, sets a
provisional grade and recomputes FSRS inline. The agent later replaces the
provisional grade (section 2): `grading` goes from `provisional` to `graded`,
with the model's feedback.

**Queue order.** Due concepts (FSRS `due_at` ≤ now, retired ones left out),
most overdue first, interleaving tracks: each round takes the most overdue
concept of every track, the most overdue of those first. **One question per
concept**: a concept with several active questions serves them in rotation
(`reps mod n`); a concept with no question is skipped. Only the newest version of
a question is active.

**Idempotency.** Both stages take an optional `client_request_id` (1–128 chars).
Stage 1 with a key already used by this user returns the first review unchanged
(same `review_id`, `locked_at`, options; the first free text stays locked); the
same key on a different question is `409`. Stage 2 resent with the stage-2 key
it was first sent with returns the stored result; any other second stage 2 is
`400 stage 2 already submitted`.

**Mastery and retirement.** A concept is *mastered* when, after a fold, its FSRS
stability is at least `MASTERY_STABILITY_DAYS` (21 days) and the latest grade was
Good or Easy; a lapse un-masters it. With the user's `retire_mastered` setting on
(the default), a mastered concept is *retired*: out of sessions and the due list,
`retired: true` on stage 2, listed in the session summary. The rule is applied
when reading, so switching the setting off brings mastered concepts back at once.
`unretire` puts a retired concept back, due now; a later answer that is still
mastered retires it again.

**Question versions (decision D8).** A rewrite from `jolt_submit_gradings` is a
new question: `version` n+1, `previous_version_id` = version n, `rewrite_reason`.
Version n is kept, marked inactive, and leaves the queue; the next session
serves version n+1 with `rewritten: true`. A rewrite of an old version extends
the chain from its newest version.

`QueueItem = {concept_id, concept_title, question_id, question_type: free_text |
pattern_match, stem, track_id, track_name, version, interval_days (days since
the last review, null on the first), review_number, rewritten, pattern_match:
PatternMatch | null}`. For a `pattern_match` question (a DSA problem, section 7)
the pattern is the answer, so it is **withheld until stage 2**: `concept_id` is
the **track id** (send it back as stage 1's `concept_id`; the server finds the
problem's pattern), `concept_title` is `"Pattern match"`, `stem` is the problem
description, and `PatternMatch = {description, io_contract ("Input: …\nOutput:
…"), io_input, io_output, examples: [{input, output, note}], difficulty: Easy |
Medium | Hard, time_weight (minutes: 2 / 4 / 7)}`. The problem's title and
LeetCode link are never served before stage 2.

`Summary = {session_id, started_at, ended_at, reviewed, correct, provisional,
graded, retired: [{concept_id, title}], moved: [{concept_id, title, old_due_at,
new_due_at}], upcoming: [{date, questions}]}`. `moved` lists each concept the
session answered whose next review changed: `old_due_at` from before its first
answer in the session, `new_due_at` now (`null` when retired). `upcoming` counts
questions due on each of the next 7 days (UTC dates, one per concept).
`reviewed` counts answers with stage 2 done.

| Method | Path | Auth | Request | Response | Errors |
|---|---|---|---|---|---|
| POST | `/v1/review-sessions` | token | `{track_id?, limit?=100 (1–500), concept_id?, kind?: notes\|dsa_patterns, time_budget_minutes? (1–240), difficulty_mix?: {easy, medium, hard}}`. `concept_id` = practise that concept now (its next question, due or not, retired or not). No `kind`: every due question (DSA patterns included); `notes`: without the DSA tracks; `dsa_patterns`: a DSA session (section 7) from the caller's enrolled DSA tracks (or `track_id`), with `time_budget_minutes` (default 30) and `difficulty_mix` (default 20 / 55 / 25). | `{id, started_at, track_id, items: [QueueItem], done_today, time_budget_minutes, time_weight_total}`. `done_today` = answers since midnight UTC. The last two are set on DSA sessions only: the clock, and the minutes the drawn problems weigh (≤ the clock). | 401; 404 `track not found` / `concept not found` / `not enrolled in this track` (DSA session for a track the caller is not enrolled in); 422 |
| POST | `/v1/review-sessions/{session_id}/end` | token | — | `Summary`. Ending again returns the same summary (recomputed, so later gradings show as `graded`). | 401, 404 `session not found` |
| GET | `/v1/reviews/due` | token | query `limit?=100` (cards) | `{count, items: [{concept_id, question_id, stem}]}`: every active question of each due card, most overdue card first, retired concepts left out; a due DSA pattern lists only the problem a session would serve, with its `concept_id` masked (the track id). Kept for compatibility; the app uses sessions. | 401, 422 |
| POST | `/v1/reviews/{question_id}/stage1` | token | `{concept_id, free_text, client_request_id?, session_id?}`. For a `pattern_match` item, `concept_id` is the queue item's (the track id); `free_text` may be empty. | `{review_id, options: [str], locked_at}`. `pattern_match`: `options` are all the track's patterns (the answer grid) and the learner's card for the pattern is created now if it is their first answer on it. | 401; 404 `question not found` (also when the concept is not the caller's: they hold no card for it; for `pattern_match`, when they are not enrolled in its track) / `session not found`; 409 key reused for another question; 422 |
| POST | `/v1/reviews/{review_id}/stage2` | token | `{selected_index, latency_ms?, client_request_id?}` | `{correct, expected_answer, correct_index, due_at, grading: provisional\|graded, provisional_grade: 1–4, interval_days (≥ 1), match, question_title, retired, feedback, concept_id, leetcode_url}`. `concept_id`: the answered concept (for `pattern_match`, the pattern the queue withheld). `pattern_match` only (else `null`): `match` `correct` (the problem's pattern) / `partial` (one of its acceptable patterns) / `incorrect`, `question_title` (the problem's title, revealed now), `leetcode_url`; `expected_answer` is the pattern's name. A `pattern_match` grade is deterministic and final at once: `grading: graded`, grade Good / Hard / Again for correct / partial / incorrect, `correct` only for `correct`, `feedback` the coaching line ("Correct: …", "Defensible: …", "Not this time: …"); it never goes to the sync's grader. | 400 `review not found` / `stage 1 not submitted…` / `stage 2 already submitted` / `selected_index is out of range` / `question not found`; 401; 422 |
| GET | `/v1/reviews/{review_id}` | token | — | `{review_id, concept_id, question_id, question_version, session_id, free_text, locked_at, selected_index, correct, provisional_grade, final_grade, grading, feedback, due_after, rewritten_question_id}`; `feedback` once graded. A `pattern_match` review shows the track id as `concept_id` until stage 2. | 401, 404 `review not found` |
| POST | `/v1/concepts/{concept_id}/unretire` | token | — | `{concept_id, retired: false, due_at}`. A concept that is not retired is left as it is. | 401, 404 `concept not found`, 409 |
| GET | `/v1/concepts/{concept_id}/questions` | token | — | `{concept_id, current_question: QuestionVersion \| null, question_versions: [QuestionVersion]}` oldest first; `QuestionVersion = {question_id, version, stem, previous_version_id, rewrite_reason, created_at, active}`. `current_question` is the one the next session serves. `GET /v1/concepts/{id}` carries the same data. A DSA pattern's problems (`pattern_match`) are its answers and are never listed (empty). | 401, 404 `concept not found` |

---

## 5. Tracks, agenda and progress

Each screen is one call (BE-07) whose Cosmos cost is fixed, whatever the number
of concepts: Tracks = 4 single-partition reads besides authentication (own
tracks, curated tracks, the caller's cards, sources); Track detail = 4 (track,
the caller's cards on it, its concepts, sources); Concept = 6 (card, concept,
track, question versions, reviews, source). Per-concept question data lives on
the card (`active_question_ids`, `question_version`, `last_answer`), kept in
step by `jolt_create_questions`, graded rewrites and reconciliation. Cards
written before BE-07 are backfilled the first time Track detail or Tracks reads
them (one query each, once).

`Track = {id, name, origin: user|jolt, syllabus: [label], agenda: Agenda,
user_id, kind: notes|dsa_patterns, archived, archived_at, concept_count,
due_count, source_count, mastered_count, retention, description, enrolled,
created_at}`. `concept_count`: the caller's live concepts on
the track (superseded ones left out). `due_count`: concepts in the review queue
now: due, not retired, with a question, **0 when the track is archived**.
`source_count`: confirmed uploads on the track. `mastered_count`: concepts at
the mastery threshold. `retention`: mean predicted FSRS recall now of the
reviewed concepts, 0–1 (0 when none was reviewed). `description`: a curated
track's catalog blurb (`null` on learners' tracks). `enrolled`: the caller
enrolled in this curated track (always `false` on their own tracks); a curated
track they are not enrolled in is paused like an archived one (`due_count` 0,
out of the queue).

**Concept state** (Track detail items, Concept): `new` (never answered) →
`mastered` (at the mastery threshold) → `due` (due now) → `learning`
(otherwise), tested in that order; an agenda item with no concept is
`not_covered`.

**Off-plan (BE-07 §5).** On a **locked** track, a concept whose key is not in
the syllabus (auto-add is skipped there) is kept and reviewed as usual, listed
on Track detail with `off_plan: true`, and left out of coverage
(`off_plan_concepts` counts it). On an unlocked track auto-add keeps the
syllabus in step; a concept logged without any key is listed but not
off-plan.

`TrackItem = {concept_key, label, parent, source: agent|user|emergent,
off_plan, concept_id, concept_title, state, next_review_at (null when retired),
question_count, question_version (newest active), found_in: {source_id,
source_title, uploaded_at} | null}`. Items follow the syllabus: one row per
concept filed under an item (an item without one is a `not_covered` row with
`concept_id: null`), then the concepts outside the syllabus (key = their
`concept_key`, or their id when they have none; label = their title; source
`emergent`).

| Method | Path | Auth | Request | Response | Errors |
|---|---|---|---|---|---|
| GET | `/v1/tracks` | token | — | `[Track]`: the caller's tracks (newest first, archived ones included and flagged), then curated ones | 401 |
| POST | `/v1/tracks` | token | `{name (1–120, trimmed), kind?: "notes", outline?: string (≤ 20 000), syllabus?: [str]}` | `Track` (counts 0). `outline` → a `draft` agenda, source `user`, with the outline kept verbatim and cut mechanically into items (one per line, bullets stripped, indentation nests; keys are slugs) for the next sync to tidy. `syllabus` → a `refined` agenda, source `user`. Neither → `none` (grows from uploads). | 401; 422 blank name, both `outline` and `syllabus`, `kind` other than `notes` (DSA tracks are curated, BE-09) |
| GET | `/v1/tracks/{track_id}` | token | — | `Track` + `{items: [TrackItem], due_today: [{concept_id, title, question_id, last_answer}], recent_sources: [{id, title, kind: photos\|pdf\|file, page_count, uploaded_at}], off_plan_count}`. `due_today`: most overdue first, `question_id` is the one the next session serves, `last_answer` the learner's latest words (null before any answer); empty when archived (or a curated track not enrolled). `recent_sources`: the track's 5 newest confirmed uploads. A curated track lists every one of Jolt's concepts on it (for DSA: the patterns, in syllabus order): `new` until the caller's first answer on it, then by their card; `question_count` is its playable problems. | 401, 404 `track not found` |
| PATCH | `/v1/tracks/{track_id}` | token | `{name?: string (1–120), archived?: bool}` | `Track`. Archiving pauses the track: its concepts leave review sessions and `due_count` until it is unarchived. | 401; 404 (not the caller's own track, curated included); 409 concurrent change; 422 |
| POST | `/v1/tracks/{track_id}/agenda/lock` | token | — | `Agenda` (status `locked`) | 401, 404 (not found, or not the caller's track) |
| POST | `/v1/tracks/{track_id}/agenda/unlock` | token | — | `Agenda` (`refined` if it has a syllabus, else `none`) | 401, 404 |
| GET | `/v1/tracks/{track_id}/coverage` | token | — | `{track_id, total_concepts, studied_concepts, decaying_concepts, off_plan_concepts, coverage_ratio, by_syllabus: [{syllabus_ref, total_concepts, studied_concepts, coverage_ratio}], uncovered_syllabus: [str]}`. Counts only the caller's concepts; off-plan ones are left out of every other count. | 401, 404 |
| GET | `/v1/progress/concepts/{concept_id}` | token | — | `{concept_id, stability, difficulty, retrievability, due_at, reps, lapses, timeline: [{reviewed_at, grade, is_correct, review_id, question_id, question_version, grading, feedback}]}`. Kept for compatibility; the app uses `GET /v1/concepts/{id}`. | 401, 404 `no state for this concept` |

### Concepts

`ConceptDetail = {id, track_id, track_name, title, summary (the concept text the
model wrote), concept_key, state: new|learning|due|mastered, retired, off_plan,
status: active|superseded, successor_id, found_in: {source_id, source_title,
page (1-based, when the model gave it), uploaded_at} | null, timeline:
[{review_id, reviewed_at, free_text, is_correct, grade (1–4, provisional then
the model's), grading: provisional|graded, feedback (once graded), question_id,
question_version, interval_days (days since the previous answer, null first)}]
(oldest first), current_question: QuestionVersion | null, question_versions:
[QuestionVersion] (oldest first), adaptive_note (the current version's
rewrite reason; null for version 1), fsrs: {stability, difficulty,
retrievability (predicted recall now, 0 before any review), due_at (null when
retired or superseded), reps, lapses}, pending_corrections: [{note,
created_at}], library: PatternLibrary | null, created_at}`. `library`: a DSA
pattern's library entry (section 7), else `null`. A curated concept is readable
before the caller's first answer on it (`state: new`, empty timeline, zero
FSRS); a DSA pattern never lists its problems in `current_question` /
`question_versions`.

| Method | Path | Auth | Request | Response | Errors |
|---|---|---|---|---|---|
| GET | `/v1/concepts/{concept_id}` | token | — | `ConceptDetail` | 401, 404 `concept not found` |
| POST | `/v1/concepts/{concept_id}/correction` | token | `{note: string (1–2000, not blank)}` | `{concept_id, note (trimmed), status: "queued", created_at}`. Stored on the caller's card (up to 20 notes); the next `jolt_sync` counts it in `pending_corrections`, `jolt_get_pending_corrections` lists it and `jolt_correct_concept` clears it. | 401, 404, 409 concurrent change, 422 |

| Tool | Arguments | Returns | Notes / errors |
|---|---|---|---|
| `jolt_get_tracks` | — | `{tracks: [{id, name, origin, kind, archived, syllabus, agenda}]}` | Read-only. |
| `jolt_get_agenda` | `track_id` | `Agenda` | `{error}` on: `track not found` (neither the caller's nor curated). |
| `jolt_set_agenda` | `track_id`, `syllabus: [{concept_key, label, parent?, source?}]`, `source?=agent`, `mark_refined?=false` | `Agenda` | Replaces the whole syllabus. An item that keeps an existing `concept_key` keeps its provenance; a new one gets the call's `source` unless it names its own. Etag-serialised with auto-add. `{error}` on: locked agenda, invalid `source`, item missing `concept_key`/`label`, `track not found` (also any curated track: read-only). |
| `jolt_get_recent_concepts` | `track_id, limit?=20` | `{concepts: [{id, text, title, syllabus_ref}]}` | Newest first; the caller's concepts only. Raises `track not found`. |
| `jolt_get_coverage` | `track_id` | `{track_id, total_concepts, studied_concepts, decaying_concepts, off_plan_concepts, coverage_ratio, uncovered_syllabus}` | Raises `track not found`. |
| `jolt_correct_concept` | `track_id, concept_id, text` | `{concept_id, updated, corrections_cleared}` | Replaces the text in place and clears the concept's pending correction notes (`corrections_cleared`). `updated: false` when the write lost a concurrency race. `{error: "concept not found"}` when the concept does not exist, is not the caller's, or is not on `track_id`. |

---

## 6. Settings

| Method | Path | Auth | Request | Response | Errors |
|---|---|---|---|---|---|
| GET | `/v1/settings` | token | — | `{retire_mastered, allow_question_rewrite}` (defaults `true`, `false`: rewriting is opt-in, decision D8). BE-08 adds the rest of the Settings screen to this body. | 401 |
| PATCH | `/v1/settings` | token | `{retire_mastered?: bool, allow_question_rewrite?: bool}` | Same as `GET` | 401, 409 concurrent change, 422 |
| POST | `/v1/settings/api-key` | token | `{provider: anthropic\|openai, key}` | `{provider, last4, status: "validated"}`. The key is checked with the provider, stored in Key Vault, and only a reference is kept in Cosmos. | 400 unsupported provider; 401; 422 provider rejected the key (string `detail`) or invalid body |
| GET | `/v1/settings/api-key` | token | — | `[{provider, last4, status: "stored"}]` | 401 |
| DELETE | `/v1/settings/api-key/{provider}` | token | — | 204 (Key Vault secret first, then the reference) | 400 unsupported provider; 401 |
| PUT | `/v1/settings/retention` | token | `{retention_days?: int\|null}` | `{retention_days}` | 401, 422 |

---

## 7. Curated tracks and the DSA pattern trainer (BE-09)

**Curated tracks (decision D7).** Jolt-authored tracks live in the same
containers as learners' tracks: `origin: "jolt"`, `user_id: null`, a `kind`
that tells the app which UI to use (`dsa_patterns` today), agenda `locked`.
Every client may read them; none may write them (rename, archive, agenda,
logging concepts, questions, corrections: 404 / the tool's not-found). A learner
**enrols** to add one; enrolment is kept on their user document and counts as
`has_track` on `GET /v1/me`. Their cards on the track are created lazily, by
their first answer. Leaving keeps the cards and answers as history but takes
the track out of the queue (it is paused, like an archived track) until they
enrol again.

| Method | Path | Auth | Request | Response | Errors |
|---|---|---|---|---|---|
| GET | `/v1/tracks/catalog` | token | — | `[Track]`: the curated tracks anyone can add (oldest first), with the caller's `enrolled` and counts | 401 |
| POST | `/v1/tracks/{track_id}/enrol` | token | — | `{track_id, enrolled: true, enrolled_at}`. Idempotent: enrolling again returns the first `enrolled_at`. | 401; 404 `curated track not found` (unknown, or a learner's own track); 409 concurrent change |
| DELETE | `/v1/tracks/{track_id}/enrol` | token | — | 204. Idempotent. | 401, 404, 409 |
| GET | `/v1/tracks/{track_id}/library` | token | — | `{track_id, patterns: [{concept_id, concept_key, name, problem_count, library: PatternLibrary}]}` in syllabus order. Readable without enrolling. | 401, 404 (not a curated track, or nothing imported yet) |
| GET | `/v1/tracks/{track_id}/decision-tree` | token | — | `{track_id, root, nodes: [{id, kind: question\|leaf, prompt, short, yes, no, pattern, note, concept_id}]}`: questions carry `prompt` (the guided walk), `short` (the flowchart box) and the `yes` / `no` node ids; leaves carry `pattern`, `note` and the pattern's `concept_id` on this track when it is one. Free (decision D3). | 401, 404 |

`PatternLibrary = {category, time, space, when_to_reach, tells: [str],
pseudocode: [str] (lines), explanation: [str] (paragraphs), animation_id}`.
`animation_id` names the pattern's step animation (v1's reel for that pattern;
the pattern's slug), `null` when there is none.

**The DSA track** (`id: "dsa"`, decision D2). Concepts = the 24 patterns (the
syllabus; `concept_key` = the pattern's slug, e.g. `arrays-and-hashing`), each
carrying its library entry. Questions = the v1 problems, type `pattern_match`:
the original description (also the `stem`), the I/O contract and examples,
`options` = all 24 patterns (the same order everywhere), `correct_index` = the
problem's pattern, acceptable patterns that score `partial`, difficulty, time
weight, near duplicate, LeetCode link; the title is stored but served only by
stage 2 (section 4). A review of a problem updates the learner's card **for its
pattern**: spaced repetition runs per pattern.

**DSA sessions** (`POST /v1/review-sessions {kind: "dsa_patterns"}`), by the v1
rules:

* **One continuous clock** (`time_budget_minutes`, default 30): problems are
  drawn until their time weights (Easy 2, Medium 4, Hard 7 minutes) fill it, so a
  hard sitting holds fewer problems. The app runs the clock; the count is
  variable.
* **Difficulty mix**: each pick orders the difficulties by a weighted random
  shuffle of `difficulty_mix` (default 20 / 55 / 25) and takes the best problem
  of the first one that still fits the time left.
* **Never two near duplicates** in one session, and no problem twice.
* **Which problem is best**: one whose pattern is due for the learner, then one
  whose pattern they have not answered yet, then one still learning; a pattern
  already drawn in this session goes behind those, and a retired (mastered)
  pattern last. Ties go to a problem not answered in the last 14 days, then at
  random.

A due pattern also joins the ordinary queue (no `kind`), one problem per
pattern, masked the same way, so Today's count includes DSA.

**Import.** `scripts/import_dsa.py --from <Jolt repo> [--include-new] [--dry-run]
[--prune] [--content export.json]` reads `data/archived/*.json` (and
`data/new/*.json` with `--include-new`, only problems whose `descriptionStatus`
is `original`), the library (`library.ts`), the decision tree
(`decisionTree.ts`) and which patterns have an animation (`animations.ts`),
validates everything like Jolt's `scripts/check-batch.mjs` (any error: nothing
is written, exit 1) and upserts by `leetcodeId` with ids derived from stable
keys, comparing every document before writing: a re-run with the same input
writes nothing. A retagged problem moves to its new pattern (the old document is
retired and kept for past reviews); a problem missing from the input stays
playable unless `--prune`. The TS sources are read directly (plain data
literals); `--content` takes a JSON export instead
(`scripts/export_dsa_from_jolt.mjs`, to become `npm run export:dsa` in Jolt).
In memory mode, `DSA_SEED_FROM=<Jolt repo>` runs the same import at startup.

---

## Testing against the API without Azure

`STORAGE_MODE=memory DEV_AUTH_ENABLED=true uvicorn jolt.main:app` serves this
whole API on in-memory fakes (add `DSA_SEED_FROM=../Jolt` for the curated DSA
track) (`jolt/data/memory.py`); sign in with
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
