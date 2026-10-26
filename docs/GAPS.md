# Screen → endpoint gaps

Each screen in the Jolt 2.0 design (Figma prototype, frames listed in
`Jolt/design/frames/index.json`; screens per pass in `Jolt/docs/v2/PLAN.md` §5 and
the FE pass briefs) compared with the API as of BE-01 ([`API.md`](./API.md)). For
each gap, the backend pass whose brief covers it.

Status: **ready** (today's endpoints are enough) · **partial** (some data
exists, the rest is missing) · **missing** (no endpoint) · **client** (no
backend needed).

Frame numbers are phone / web / tablet.

## Onboarding (FE-03)

| Screen | Frames | Needs from the backend | Today | Status | Covered by |
|---|---|---|---|---|---|
| Splash, Welcome | 1, 2 / 21 | — | — | client | — |
| Sign in (Google) | 2 / 21 | `POST /v1/auth/google` → app token + user | Only anonymous `POST /v1/accounts` | missing | BE-03 |
| Connect your model | 3 / 21 | Create an MCP token shown once, MCP server URL, per-client steps, nightly prompt | Token only from account creation, same token for app and MCP | missing | BE-03 (`/v1/mcp-tokens`, `/v1/me`), BE-04 (`docs/CONNECT.md`, `GET /v1/sync/prompt`) |
| First track | 4 / 21 | Create a notes track with name and optional outline (→ `draft`); or enrol in curated DSA | `POST /v1/tracks {name, kind: notes, outline?}` → `draft` agenda with the outline (BE-07) | partial | Curated catalog and enrol: BE-09 |
| Waiting for first sync | — / 21 | Poll sync status; resume point for a returning user | — | missing | BE-04 (`GET /v1/sync/status`), BE-03 (`GET /v1/me` onboarding state) |

## Today (FE-04)

| Screen part | Frames | Needs | Today | Status | Covered by |
|---|---|---|---|---|---|
| Due card | 5 / 22 / 37 | Due count and per-track breakdown | `GET /v1/tracks` `due_count` per track (BE-07); the total is their sum | ready | — |
| Sync line | 5 / 22 / 37 | Last sync time, client, late / never | — | missing | BE-04 |
| Stats | 5 / 22 / 37 | Streak, retention, reviews this week | — | missing | BE-08 (`GET /v1/progress`) |
| Tracks | 5 / 22 / 37 | Concepts, due and sources count per track | `GET /v1/tracks` `concept_count`, `due_count`, `source_count` (BE-07) | ready | — |
| Tonight's uploads | 5 / 22 / 37 | Unprocessed sources with their track | `GET /v1/sources?status=unprocessed` with `track_id`, `track_name`, `title`, `page_count` (BE-05) | ready | — |
| What's new | 5 / 22 / 37 | Concepts added by the last sync, with the source date ("Found in your 12 Sep notes") | Track detail items carry `found_in` (BE-07); no "since the last sync" list | missing | **No brief covers it.** BE-07 did not add `GET /v1/concepts?since=` (outside its brief); BE-04's sync runs recording concept ids remains the proposal |
| Empty / first-run states | — | Same data as above, plus `GET /v1/me` | — | missing | BE-03, BE-04 |

## Review (FE-06)

| Screen | Frames | Needs | Today | Status | Covered by |
|---|---|---|---|---|---|
| Queue / session start | — | Ordered queue interleaving tracks, most overdue first | `POST /v1/review-sessions {track_id?, limit?, concept_id?, kind?}` → `{id, started_at, track_id, items, done_today}` (BE-06) | ready | Daily cap: BE-08 |
| Review 1 · Your own words | 6 / 23 / 38 | Stage 1 locks the text, then releases options; idempotent on a client request id | `POST /v1/reviews/{question_id}/stage1 {…, client_request_id?, session_id?}` → `locked_at` (BE-06) | ready | — |
| Review 2 · Pick the best match | 7 / 24 / 39 | Options only after stage 1 | Returned by stage 1 only; queue items carry the stem only | ready | — |
| Review 3 · Result | 8 / 25 / 40 | Correct, expected answer, next due date, provisional grade, "question rewritten" note | Stage 2 returns `grading`, `provisional_grade`, `interval_days`, `retired`, `feedback`; idempotent (BE-06). The "rewritten" note is on the *queue item* of the next session (`rewritten`, `version`): a rewrite only happens at the nightly grading, after the result screen | ready | — |
| Review 4 · Session complete | 9 / 26 | Summary with "what moved" (old and new due dates) | `POST /v1/review-sessions/{id}/end` → reviewed, correct, provisional / graded, retired, moved, upcoming (BE-06) | ready | — |

## Tracks and concepts (FE-07)

| Screen | Frames | Needs | Today | Status | Covered by |
|---|---|---|---|---|---|
| Tracks | 10 / 27 / 41 | List with counts, `kind`, curated badge | `GET /v1/tracks`: counts, `kind`, `archived`, `retention`, `origin` (the curated badge); `PATCH` rename / archive (BE-07) | ready | Curated `description`, enrol: BE-09 |
| New track sheet | 10 / 27 / 41 | Name and optional outline → `draft` agenda | `POST /v1/tracks {name, kind, outline}` (BE-07) | ready | — |
| Track detail | 11 / 28 / 42 | One call: agenda items with state (new / learning / due / mastered / not covered), next review, question count; due today; recent sources | `GET /v1/tracks/{id}` (BE-07): 4 Cosmos reads, whatever the size | ready | — |
| Syllabus mode pill and overlay | 11 / 28 / 42, 54 | `agenda.status` | In every `Track` | ready | — |
| Lock toggle | 11 / 28 / 42 | Lock / unlock | `POST /v1/tracks/{id}/agenda/lock` and `/unlock` | ready | — |
| "Added to the syllabus from your notes" | 11 / 28 / 42 | Per-item provenance (auto-added vs authored) | Per-item `source` on every agenda item and Track detail row (BE-07) | ready | — |
| Concept | 12 / 29 / 43 | Summary, source and page, answer timeline (date, your words, correct, grade, feedback, question version), adaptive question note, FSRS state | `GET /v1/concepts/{id}` (BE-07); `POST /v1/concepts/{id}/unretire` (BE-06) | ready | `page` is set only when the agent passes it (`jolt_log_session` / reconciliation `page`) |
| Feedback panel gate | 12 / 29 / 43 | `has_api_key` | `GET /v1/settings/api-key` (works as a stand-in) | partial | BE-08 (`has_api_key` on `/v1/me`); panel content BE-11 |
| "Something's wrong with this concept" | 12 / 29 / 43 | Store a correction note for the next sync | `POST /v1/concepts/{id}/correction` → `jolt_sync.pending_corrections`, `jolt_get_pending_corrections`, `jolt_correct_concept` (BE-07) | ready | — |
| DSA track (placeholder) | 13 / 30 / 44 | Curated track, enrol, patterns as syllabus, `pattern_match` questions, decision tree, library | — | missing | BE-09 |

## Sources and reader (FE-05, FE-08)

| Screen | Frames | Needs | Today | Status | Covered by |
|---|---|---|---|---|---|
| Add to Jolt sheet | 52 | Upload N pages to a track as one source (decision A7), with size and hash checks; accepted types and size limit | `POST /v1/uploads/request` with `track_id`, `title`, `pages[]` → one PUT URL per page, `max_bytes_per_file`, `accepted_types`; `confirm` checks each page's size and SHA-256 (BE-05) | ready | — |
| Sources list / table | 14 / 31 / 45 | Title, track, page count, uploaded at, status incl. `failed`, concepts found, questions written, `low_confidence`; filters by track and status | `GET /v1/sources?track_id=&status=` (BE-05) | ready | — |
| Source · Original | 15 | Signed GET URL per page | `GET /v1/sources/{id}` `pages[].url` (BE-05) | ready | — |
| Source · What it found | 16 / 32 / 46 | Active extraction markdown | `GET /v1/sources/{id}` `active_read.markdown`, `concepts` (BE-05) | ready | — |
| Source · Past reads | 17 / 33 / 47 | Every read: model, time, concepts found, questions written | `GET /v1/sources/{id}` `reads[]`: number, model, client label, time, confidence, concepts found, questions written (BE-05) | ready | — |
| Read again sheet | 53 | Queue a re-read with an optional preferred model; the next `jolt_sync` plan lists it | `POST /v1/sources/{id}/reread`; `jolt_sync` `rereads[]`, `jolt_get_unprocessed_sources` `supersede`/`preferred_model` (BE-05) | ready | BE-04 may fold `rereads` into its richer plan |
| Reader | 18 / 34 / 48 | Page URLs; screen-size images first, full size on zoom; thumbnails on web | `GET /v1/sources/{id}` `pages[].url`: the **originals** (BE-05 decision, below) | partial | Renditions: no pass (see below) |
| Toasts | 56, 57 | — | — | client | — |

**BE-05 decisions and follow-ups for the sources screens**

- **Reader renditions: originals only.** The Reader gets a signed GET of each
  uploaded file. No server-side resizing or thumbnails: that needs an image
  pipeline (decode HEIC/PDF, write renditions) and a background job, which the
  single-service design avoids, and Jolt compute never reads uploads except to
  hash them at confirm. Consequences for the front end: phone photos are a few MB
  each, so lazy-load pages and use the browser's own scaling; **HEIC does not
  display outside Safari**, so the app should convert HEIC to JPEG before upload
  (or show a "preview not available" tile); a PDF is one page entry
  (`content_type: application/pdf`) that the Reader renders itself. Revisit with
  a rendition step (e.g. an Azure Function on blob-created) if the Reader is slow
  on mobile data.
- **A PDF counts as one page.** `page_count` is the number of uploaded files; the
  server does not parse PDFs.
- **Abandoned uploads are not cleaned up.** A requested but never confirmed
  source keeps its row (hidden from every view and from syncs) and any bytes that
  were PUT. A Blob lifecycle rule or a cleanup on the next request would fix it.
  → BE-10 (data hygiene) or BE-08 (data retention).
- **Failure is detected, not reported.** `failed` comes from lapsed sync leases
  (`SOURCE_MAX_ATTEMPTS`); an agent cannot yet say "this file is unreadable"
  directly. A `jolt_report_source_failure` tool would make it immediate. → BE-04
  (sync plan and errors).
- **`preferred_model` is a hint.** Any connected client may answer a read-again
  request; the plan shows the preference.

**BE-06 decisions and follow-ups for the review screens**

- **One question per concept per session**, rotating through the concept's
  active questions. Answering several questions of one concept on one day would
  fold several same-day reviews into FSRS. The FE-02 mock serves every active
  question; the item shape is the same.
- **"Today" is the UTC day** for `done_today` and `upcoming`: the server does
  not know the user's time zone yet. → BE-08 (a time zone setting) if it matters.
- **`time_budget_minutes` and `kind: dsa_patterns`** are accepted; DSA sessions
  are empty until BE-09 adds DSA tracks and `pattern_match` questions (`match`,
  `question_title`, `pattern_match` are `null` until then).
- **Mastery threshold**: FSRS stability ≥ `MASTERY_STABILITY_DAYS` (21) after a
  Good or Easy answer, the same number as the FE mock. Retirement is evaluated
  when reading (setting on/off takes effect at once, no rewrite of cards).
- **Rewrites happen at grading, not at the result screen.** The result screen
  cannot say "rewritten"; the next session's queue item does (`rewritten`).
  Today → What's new could list rewrites from BE-04's sync runs.
- **No daily review cap yet.** → BE-08.

**BE-07 decisions and follow-ups for the tracks and concept screens**

- **Off-plan concepts** (brief §5): kept, reviewed as usual, flagged `off_plan`
  on Track detail and left out of coverage, only on *locked* tracks (where
  auto-add is skipped). A concept logged with no key on an unlocked track is
  listed as an unfiled row, not off-plan.
- **Archiving pauses a track**: its concepts leave review sessions and
  `due_count` is 0 until unarchived. The track stays in `GET /v1/tracks`
  (flagged); the app hides or greys it.
- **Learners create `notes` tracks only.** `kind: dsa_patterns` is refused
  (422): DSA tracks are curated (BE-09).
- **The outline is cut mechanically** into draft items (one per line,
  indentation nests, keys are slugs) so Track detail has rows at once; the model
  tidies it at the next sync (`jolt_set_agenda`, `mark_refined`). The backend
  still never shapes a syllabus.
- **"No N+1" is a fixed number of reads**, not one: Tracks and Track detail are
  4 single-partition reads, Concept 6 (besides authentication), tested by
  counting Cosmos calls at two sizes. Per-concept question data is denormalised
  on the card. Cards from before BE-07 are backfilled on first read (one query
  each, once).
- **Corrections wait on the card.** Up to 20 notes per concept; applying
  `jolt_correct_concept` (even with unchanged text) clears them all, including
  a note added after the agent read the list (rare race; the learner can add it
  again).
- **Reconciliation keeps old reviews on the old concept.** A `changed` concept's
  successor inherits the card state but not the reviews (they were answers to
  the old wording); they are not stamped `invalidated_by_extraction_id` (LLD
  §7.7) because that would make a step's writes unbounded. A successor's mastery
  must be re-earned by its own answers.
- **`enrolled`** is `true` on a curated track where the caller has concepts;
  BE-09 replaces it with real enrolment.

## Progress and settings (FE-09)

| Screen part | Frames | Needs | Today | Status | Covered by |
|---|---|---|---|---|---|
| Progress | 19 / 35 / 49 | Retention and weekly series, reviews, streak, learning / mastered, daily heatmap, per-track table | Only per-concept `GET /v1/progress/concepts/{id}` | missing | BE-08 (`GET /v1/progress`) |
| Settings · Model | 20 / 36 / 50 | MCP status, last sync and health, connected clients (create / revoke tokens), nightly prompt | — | missing | BE-03 (tokens), BE-04 (sync status, prompt), BE-08 (model status block) |
| Settings · API key + sheet | 20 / 36 / 50, 51, 58 | Add, list (`added_at`), remove | `POST`, `GET` (no `added_at`), `DELETE /v1/settings/api-key` | partial | BE-08 |
| Settings · Appearance | 20 / 36 / 50 | Theme | — | missing | BE-08 (`GET/PATCH /v1/settings`) |
| Settings · Reviews | 20 / 36 / 50 | Desired retention (FSRS), daily cap, retire mastered, allow question rewrite | `GET` / `PATCH /v1/settings {retire_mastered, allow_question_rewrite}` with their behaviour (BE-06); desired retention is still server-wide (`FSRS_DESIRED_RETENTION`), no daily cap | partial | BE-08 (desired retention, daily cap, the rest of the body) |
| Settings · Data | 20 / 36 / 50, 59 | Storage used, export, delete account | `GET /v1/storage` `{bytes_used, source_count, page_count}` (BE-05); `PUT /v1/settings/retention` stores a data-retention preference in days that nothing uses yet | partial | BE-08 (export, `DELETE /v1/me`) |

## After the MVP (FE-12)

| Feature | Needs | Status | Covered by |
|---|---|---|---|
| Concept feedback, result "Explain" | `POST /v1/concepts/{id}/feedback` on the user's key | missing | BE-11 |
| Read now with my key | `POST /v1/sources/{id}/read-now` | missing | BE-11 |

---

## Issues found in the existing API (not screen gaps)

To fix in the pass named, or sooner.

1. ~~**Ownership checks on track- and concept-keyed calls.**~~ **Fixed in BE-07**
   (decision A16): every concept-, question-, track- and source-keyed MCP tool
   and REST route goes through `jolt/services/ownership.py` (a concept is the
   caller's when they hold its card; writes need the caller's own track, so
   curated tracks are read-only; curated tracks show each learner only their own
   concepts). `tests/test_ownership.py` covers every such tool and route. To be
   confirmed in the BE-10 security review.
2. ~~**Supersede does not reconcile concepts.**~~ **Fixed in BE-07**:
   `jolt_store_extraction(concepts=...)` reconciles (unchanged / refined /
   changed / removed / new) with a documented ceiling of
   `RECONCILE_MAX_CONCEPTS` (25) steps per call and a resumable checkpoint
   (`jolt_continue_reconciliation`); Past reads' `is_new` is now meaningful. A
   supersede *without* `concepts` still only moves the pointer.
3. ~~**`jolt_correct_concept` description vs behaviour.**~~ **Fixed in BE-07**:
   the description now says `{error: "concept not found"}`; the tool no longer
   revives a superseded concept, and clears pending correction notes.
4. **`PUT /v1/settings/retention` naming.** It stores *data* retention in days,
   which is easy to confuse with FSRS *desired retention* (Settings · Reviews). →
   BE-08 should fold it into `PATCH /v1/settings` with explicit names.
5. **Token lookup scans every user** (Argon2 verify per user). → BE-03.
6. **`GET /v1/reviews/due` `limit`** counts cards, not questions; a card with
   several active questions returns them all. Kept as it is for compatibility
   (it now leaves out retired concepts); review sessions (BE-06) serve one
   question per concept and are what the app uses.
