# Jolt API reference

The human-readable view of the contract. The machine-readable source of truth is
generated and checked in CI:

- [`contract/openapi.json`](../contract/openapi.json) — REST, every `/v1` route.
- [`contract/mcp-tools.json`](../contract/mcp-tools.json) — every MCP tool: name,
  full agent-facing description, input schema, output schema.

Regenerate both with `python scripts/export_contract.py` after changing a route, a
tool, a tool description or `jolt/mcp/schemas.py`. This file is written by hand:
update it in the same change.

State: API version 0.2.0, the MVP plus the track agenda model (PR #3), sources
(BE-05), the review loop (BE-06) and the tracks and concept read models with
supersede reconciliation (BE-07). Screens that have no endpoint yet are listed
in [`GAPS.md`](./GAPS.md).

## Conventions

| | |
|---|---|
| **Base paths** | REST under `/v1`. MCP over HTTP+SSE: connect to `/mcp/sse`, post to `/mcp/messages/`. `GET /health` → `{status: "ok", version}` (no auth). CORS: the origins in `CORS_ORIGINS` / `CORS_ORIGIN_REGEX`, no credentials (the token is a header). |
| **Auth** | `Authorization: Bearer <token>` on every `/v1` route except `POST /v1/accounts`, and on the MCP connection. Today there is one token kind (`jolt_live_…`) for both surfaces; BE-03 splits app and MCP tokens. A bare token without `Bearer` is also accepted. |
| **Scope** | Identity comes only from the token. No route or tool takes a `user_id`. |
| **Ownership** (BE-07, A16) | A **track** is *visible* when it is the caller's or curated, *owned* only when it is the caller's: reads take a visible track, writes (agenda, rename, archive, logging concepts) an owned one, so curated tracks are read-only. A **concept** is the caller's when they hold its card (FSRS state); on a curated track each learner sees only their own concepts. A **question** is the caller's when its concept is. Anything else answers exactly like an unknown id (404 / the tool's not-found). |
| **Errors (REST)** | Non-2xx bodies are `{"detail": "<message>"}` (`ErrorResponse`). Request-validation failures are `422` with `{"detail": [ {loc, msg, type} ]}` (`HTTPValidationError`). `401` always carries `WWW-Authenticate: Bearer`. |
| **Errors (MCP)** | Tools marked *`{error}` on* return `{"error": "<message>"}` for the listed recoverable cases. Every other failure (missing/invalid token, unknown source, grade outside 1–4) is raised and arrives as an MCP tool error (`isError: true`). |
| **Types** | IDs are opaque strings (32-hex today). Timestamps are ISO-8601 UTC. FSRS grades: 1 Again, 2 Hard, 3 Good, 4 Easy. |
| **Agenda** | `{status: none\|draft\|refined\|locked, source: agent\|user\|emergent, syllabus: [{concept_key, label, parent, source}], last_refined_at, refined_from_source_id, outline}`; written `Agenda` below. An item's `source` is who added it (`user`: the learner's outline or syllabus, `agent`: a refine, `emergent`: auto-added from notes); items written before BE-07 report the agenda's `source`. `outline` is the learner's raw outline from track creation (else `null`). |

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
`jolt_get_pending_gradings` → `jolt_submit_gradings`; then
`jolt_get_pending_corrections` → `jolt_correct_concept`, and tidy any `draft`
agenda with an `outline` (`jolt_set_agenda`, `mark_refined`). A read-again
(`supersede: true`) passes its concepts to `jolt_store_extraction` instead of
`jolt_log_session` (reconciliation, below). FSRS is recomputed inline on grade
submission; expired leases are reclaimed lazily by the claim query (no
background job).

| Tool | Arguments | Returns | Notes / errors |
|---|---|---|---|
| `jolt_sync` | `limit?=50` | `{unprocessed_sources, pending_gradings, pending_corrections, rereads: [{source_id, title, preferred_model, requested_at}]}` | Read-only. `pending_corrections` counts concepts with learner notes waiting (BE-07). Counts only claimable, confirmed sources. `rereads` are the sources the user asked to read again (also in the count); re-extract each with `supersede: true`. `preferred_model` (e.g. `claude`, `codex`, or `null`) is a hint, not a lock. |
| `jolt_get_unprocessed_sources` | `limit?=10` | `{sources: [{source_id, filename, content_type, lease_expires_at, title, track_id, page_count, supersede, preferred_model}]}` | Claims each source (`in_flight`, lease of `LEASE_TTL_SECONDS`, 900 s by default). Etag-guarded: a concurrent claimer gets fewer rows. Only confirmed uploads. `supersede: true` = a read-again request on a source that already has a read. A source whose lease lapses `SOURCE_MAX_ATTEMPTS` (3) times is marked `failed` instead of claimed. |
| `jolt_get_source_content` | `source_id` | `{source_id, filename, content_type, read_url, page_count, pages: [{index, filename, content_type, read_url}]}` | One signed GET per page, valid `BLOB_SAS_TTL_SECONDS`; the top-level fields are page 0's. Raises if the source is not the caller's. |
| `jolt_store_extraction` | `source_id, markdown, confidence?=0, model_id?, supersede?=false, concepts?: [{text, track_id?, title?, concept_key?, syllabus_ref?, parent?, page?}]` | `{extraction_id, status: "active", reconciliation: Reconciliation \| null}` *`{error}` on* | Marks the source `processed` (or back to `unprocessed` if a read-again request arrived while the sync held it) and points it at the new extraction. The previous active extraction always becomes `superseded` (one active read per source). Records the MCP token's label as the read's `client_label`. With `concepts`, reconciles them against the replaced read's concepts (below); `track_id` defaults to the source's track. `{error}` (nothing stored) on: a concept with no text, or no `track_id` when the source has no track. Raises if the source, or a concept's track, is not the caller's. |
| `jolt_continue_reconciliation` | `source_id` | `Reconciliation` *`{error}` on* | Resumes a `partial` reconciliation of the source's active read from its checkpoint (up to the ceiling again). A `complete` one is returned unchanged. `{error}` when the active read has none (stored without `concepts`). Raises if the source is not the caller's. |
| `jolt_log_session` | `source_ids: [str]`, `concepts: [{text, track_id, title?, concept_key?, syllabus_ref?, parent?, source_id?, page?}]` | `{session_id, concept_ids}` (input order) | Creates concepts plus an FSRS card due now, and auto-adds unknown `concept_key`s to each track's agenda (skipped on locked agendas). `page` is the 1-based source page (Concept → "where found"). Raises, writing nothing, if a `track_id` is not one of the caller's own tracks (curated ones are refused) or a source id is not theirs. Not idempotent. |
| `jolt_create_questions` | `concept_id`, `questions: [{stem, options: [str], correct_index, expected_answer}]` | `{question_ids}` (input order) | Raises if the concept is not the caller's. Not idempotent. |
| `jolt_get_pending_gradings` | `limit?=10` | `{gradings: [{review_id, concept_id, question_id, free_text, expected_answer, stem, options, correct_index, question_version, selected_index, is_correct}], allow_question_rewrite}` | Reviews with stage 1 submitted and no final grade. The question fields are the version the learner answered (BE-06), so the agent can rewrite it; `allow_question_rewrite` is the user's opt-in (decision D8). |
| `jolt_get_pending_corrections` | — | `{corrections: [{concept_id, track_id, title, text, notes: [{note, created_at}]}]}` | The learner's notes from `POST /v1/concepts/{id}/correction`, oldest request first. Answer each with `jolt_correct_concept`. Read-only. |
| `jolt_submit_gradings` | `gradings: [{review_id, suggested_fsrs_grade: 1–4, rationale?, rubric_version?, rewritten_question?: {stem, options, correct_index, expected_answer, rewrite_reason?}}]` | `{applied, submitted, rewritten: [{review_id, question_id, version, previous_version_id}], rewrites_skipped: [{review_id, reason}]}` | Idempotent per review. Re-folds each affected card inline (FSRS and mastery). `rationale` is the feedback the learner sees. A `rewritten_question` becomes version n+1 of the question only when the user's `allow_question_rewrite` is on (section 4, question versions); otherwise, or when invalid, it is listed in `rewrites_skipped` and the grade still lands. A retry lists the version it created again, never a second one. Raises on a grade outside 1–4. |

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
| POST | `/v1/sources/{source_id}/reread` | token | `{preferred_model?: string\|null}` (≤ 64 chars, e.g. `claude`, `codex`) | `{source_id, requested_at, preferred_model}`. The next `jolt_sync` lists it in `rereads`; a request made while a sync holds the source survives that sync | 401, 404 |
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

`QueueItem = {concept_id, concept_title, question_id, question_type: "free_text"
(BE-09 adds "pattern_match"), stem, track_id, track_name, version, interval_days
(days since the last review, null on the first), review_number, rewritten,
pattern_match: null (BE-09)}`

`Summary = {session_id, started_at, ended_at, reviewed, correct, provisional,
graded, retired: [{concept_id, title}], moved: [{concept_id, title, old_due_at,
new_due_at}], upcoming: [{date, questions}]}`. `moved` lists each concept the
session answered whose next review changed: `old_due_at` from before its first
answer in the session, `new_due_at` now (`null` when retired). `upcoming` counts
questions due on each of the next 7 days (UTC dates, one per concept).
`reviewed` counts answers with stage 2 done.

| Method | Path | Auth | Request | Response | Errors |
|---|---|---|---|---|---|
| POST | `/v1/review-sessions` | token | `{track_id?, limit?=100 (1–500), concept_id?, kind?: notes\|dsa_patterns, time_budget_minutes?}`. `concept_id` = practise that concept now (its next question, due or not, retired or not). `kind: dsa_patterns` is empty until BE-09; `time_budget_minutes` is accepted and ignored until BE-09. | `{id, started_at, track_id, items: [QueueItem], done_today}`. `done_today` = answers since midnight UTC. | 401; 404 `track not found` / `concept not found`; 422 |
| POST | `/v1/review-sessions/{session_id}/end` | token | — | `Summary`. Ending again returns the same summary (recomputed, so later gradings show as `graded`). | 401, 404 `session not found` |
| GET | `/v1/reviews/due` | token | query `limit?=100` (cards) | `{count, items: [{concept_id, question_id, stem}]}`: every active question of each due card, most overdue card first, retired concepts left out. Kept for compatibility; the app uses sessions. | 401, 422 |
| POST | `/v1/reviews/{question_id}/stage1` | token | `{concept_id, free_text, client_request_id?, session_id?}` | `{review_id, options: [str], locked_at}` | 401; 404 `question not found` (also when the concept is not the caller's: they hold no card for it) / `session not found`; 409 key reused for another question; 422 |
| POST | `/v1/reviews/{review_id}/stage2` | token | `{selected_index, latency_ms?, client_request_id?}` | `{correct, expected_answer, correct_index, due_at, grading: provisional\|graded, provisional_grade: 1–4, interval_days (≥ 1), match: null, question_title: null, retired, feedback}`. `match` / `question_title` are for BE-09 `pattern_match` questions. | 400 `review not found` / `stage 1 not submitted…` / `stage 2 already submitted` / `selected_index is out of range` / `question not found`; 401; 422 |
| GET | `/v1/reviews/{review_id}` | token | — | `{review_id, concept_id, question_id, question_version, session_id, free_text, locked_at, selected_index, correct, provisional_grade, final_grade, grading, feedback, due_after, rewritten_question_id}`; `feedback` once graded | 401, 404 `review not found` |
| POST | `/v1/concepts/{concept_id}/unretire` | token | — | `{concept_id, retired: false, due_at}`. A concept that is not retired is left as it is. | 401, 404 `concept not found`, 409 |
| GET | `/v1/concepts/{concept_id}/questions` | token | — | `{concept_id, current_question: QuestionVersion \| null, question_versions: [QuestionVersion]}` oldest first; `QuestionVersion = {question_id, version, stem, previous_version_id, rewrite_reason, created_at, active}`. `current_question` is the one the next session serves. `GET /v1/concepts/{id}` carries the same data. | 401, 404 `concept not found` |

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
due_count, source_count, mastered_count, retention, description (null until
BE-09), enrolled, created_at}`. `concept_count`: the caller's live concepts on
the track (superseded ones left out). `due_count`: concepts in the review queue
now: due, not retired, with a question, **0 when the track is archived**.
`source_count`: confirmed uploads on the track. `mastered_count`: concepts at
the mastery threshold. `retention`: mean predicted FSRS recall now of the
reviewed concepts, 0–1 (0 when none was reviewed). `enrolled`: curated tracks
on which the caller has concepts (always `false` on their own tracks).

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
| GET | `/v1/tracks/{track_id}` | token | — | `Track` + `{items: [TrackItem], due_today: [{concept_id, title, question_id, last_answer}], recent_sources: [{id, title, kind: photos\|pdf\|file, page_count, uploaded_at}], off_plan_count}`. `due_today`: most overdue first, `question_id` is the one the next session serves, `last_answer` the learner's latest words (null before any answer); empty when archived. `recent_sources`: the track's 5 newest confirmed uploads. A curated track shows only the caller's concepts. | 401, 404 `track not found` |
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
created_at}], created_at}`.

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
