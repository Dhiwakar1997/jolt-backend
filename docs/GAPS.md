# Screen → endpoint gaps

Each screen in the Jolt 2.0 design (Figma prototype, frames listed in
`Jolt/design/frames/index.json`; screens per pass in `Jolt/docs/v2/PLAN.md` §5 and
the FE pass briefs) compared with the API as of BE-10 (BE-07 to BE-10 merged, API 0.9.0) ([`API.md`](./API.md)). For
each gap, the backend pass whose brief covers it.

Status: **ready** (today's endpoints are enough) · **partial** (some data
exists, the rest is missing) · **missing** (no endpoint) · **client** (no
backend needed).

Frame numbers are phone / web / tablet.

## Onboarding (FE-03)

| Screen | Frames | Needs from the backend | Today | Status | Covered by |
|---|---|---|---|---|---|
| Splash, Welcome | 1, 2 / 21 | — | — | client | — |
| Sign in (Google) | 2 / 21 | `POST /v1/auth/google` → app token + user | `POST /v1/auth/google` (BE-03); dev mode: `POST /v1/auth/dev` | ready | — |
| Connect your model | 3 / 21 | Create an MCP token shown once, MCP server URL, per-client steps, nightly prompt | `POST /v1/mcp-tokens` (token shown once), `mcp_server_url` on `GET /v1/me` (BE-03); per-client steps in `docs/CONNECT.md` (static copy for the screen; Claude's connector URL is `mcp_server_url?token=<token>`), prompt from `GET /v1/sync/prompt` (BE-04) | ready | — (client setups **deploy pending**, see `CONNECT.md`) |
| First track | 4 / 21 | Create a notes track with name and optional outline (→ `draft`); or enrol in curated DSA | `POST /v1/tracks {name, kind: notes, outline?}` → `draft` agenda with the outline (BE-07); `GET /v1/tracks/catalog` + `POST /v1/tracks/{id}/enrol` (BE-09), which counts as `has_track` | ready | — |
| Waiting for first sync | — / 21 | Poll sync status; resume point for a returning user | `GET /v1/sync/status` (`health: never` until the first run), `GET /v1/me` `onboarding.first_sync_at` (set when the first run closes) | ready | — |

## Today (FE-04)

| Screen part | Frames | Needs | Today | Status | Covered by |
|---|---|---|---|---|---|
| Due card | 5 / 22 / 37 | Due count and per-track breakdown | `GET /v1/tracks` `due_count` per track (BE-07); the total is their sum | ready | — |
| Sync line | 5 / 22 / 37 | Last sync time, client, late / never | `GET /v1/sync/status`: `last_run` (`started_at`, `finished_at`, `client_label`, counts), `health`, `next_expected_at` (pass `?tz=`) | ready | — |
| Stats | 5 / 22 / 37 | Streak, retention, reviews this week | `GET /v1/progress?weeks=1`: `streak_days`, `retention`, `reviews_this_week` (BE-08) | ready | — |
| Tracks | 5 / 22 / 37 | Concepts, due and sources count per track | `GET /v1/tracks` `concept_count`, `due_count`, `source_count` (BE-07) | ready | — |
| Tonight's uploads | 5 / 22 / 37 | Unprocessed sources with their track | `GET /v1/sources?status=unprocessed` with `track_id`, `track_name`, `title`, `page_count` (BE-05) | ready | — |
| What's new | 5 / 22 / 37 | Concepts added by the last sync, with the source date ("Found in your 12 Sep notes") | `changes[]` on each sync run (`GET /v1/sync/status` → `last_run`, `GET /v1/sync/runs`): `concept_added` / `concept_updated` / `question_rewritten` with `concept_title`, `track_id`, `source_id`, `source_uploaded_at`, `added_to_syllabus` (decision A25, BE-04); concepts a read-again reconciliation adds or refines, and concepts a correction rewrites, are changes too (BE-07). Track detail items also carry `found_in` (BE-07) | ready | — |
| Empty / first-run states | — | Same data as above, plus `GET /v1/me` | `GET /v1/me` (BE-03), sync status (BE-04), `GET /v1/sources` (BE-05), `GET /v1/tracks` counts (BE-07), stats (BE-08) | ready | — |

## Review (FE-06)

| Screen | Frames | Needs | Today | Status | Covered by |
|---|---|---|---|---|---|
| Queue / session start | — | Ordered queue interleaving tracks, most overdue first; daily cap | `POST /v1/review-sessions {track_id?, limit?, concept_id?, kind?}` → `{id, started_at, track_id, items, done_today, daily_review_cap, capped}` (BE-06, cap BE-08) | ready | — |
| Review 1 · Your own words | 6 / 23 / 38 | Stage 1 locks the text, then releases options; idempotent on a client request id | `POST /v1/reviews/{question_id}/stage1 {…, client_request_id?, session_id?}` → `locked_at` (BE-06) | ready | — |
| Review 2 · Pick the best match | 7 / 24 / 39 | Options only after stage 1 | Returned by stage 1 only; queue items carry the stem only | ready | — |
| Review 3 · Result | 8 / 25 / 40 | Correct, expected answer, next due date, provisional grade, "question rewritten" note | Stage 2 returns `grading`, `provisional_grade`, `interval_days`, `retired`, `feedback`; idempotent (BE-06). The "rewritten" note is on the *queue item* of the next session (`rewritten`, `version`): a rewrite only happens at the nightly grading, after the result screen | ready | — |
| Review 4 · Session complete | 9 / 26 | Summary with "what moved" (old and new due dates) | `POST /v1/review-sessions/{id}/end` → reviewed, correct, provisional / graded, retired, moved, upcoming (BE-06) | ready | — |

## Tracks and concepts (FE-07)

| Screen | Frames | Needs | Today | Status | Covered by |
|---|---|---|---|---|---|
| Tracks | 10 / 27 / 41 | List with counts, `kind`, curated badge | `GET /v1/tracks`: counts, `kind`, `archived`, `retention`, `origin` (the curated badge); `PATCH` rename / archive (BE-07); curated `description` and real `enrolled` (BE-09) | ready | — |
| New track sheet | 10 / 27 / 41 | Name and optional outline → `draft` agenda | `POST /v1/tracks {name, kind, outline}` (BE-07) | ready | — |
| Track detail | 11 / 28 / 42 | One call: agenda items with state (new / learning / due / mastered / not covered), next review, question count; due today; recent sources | `GET /v1/tracks/{id}` (BE-07): 4 Cosmos reads, whatever the size | ready | — |
| Syllabus mode pill and overlay | 11 / 28 / 42, 54 | `agenda.status` | In every `Track` | ready | — |
| Lock toggle | 11 / 28 / 42 | Lock / unlock | `POST /v1/tracks/{id}/agenda/lock` and `/unlock` | ready | — |
| "Added to the syllabus from your notes" | 11 / 28 / 42 | Per-item provenance (auto-added vs authored) | Per-item `source` on every agenda item and Track detail row (BE-07) | ready | — |
| Concept | 12 / 29 / 43 | Summary, source and page, answer timeline (date, your words, correct, grade, feedback, question version), adaptive question note, FSRS state | `GET /v1/concepts/{id}` (BE-07); `POST /v1/concepts/{id}/unretire` (BE-06) | ready | `page` is set only when the agent passes it (`jolt_log_session` / reconciliation `page`) |
| Feedback panel gate | 12 / 29 / 43 | `has_api_key` | `has_api_key` on `GET /v1/me` (added in BE-03 for BE-08) | ready | panel content BE-11 |
| "Something's wrong with this concept" | 12 / 29 / 43 | Store a correction note for the next sync | `POST /v1/concepts/{id}/correction` → `jolt_sync.pending_corrections` (the plan is not `done` while notes wait, and lists `jolt_get_pending_corrections` → `jolt_correct_concept`), the changed text is a `concept_updated` change on the run (BE-07) | ready | — |
| DSA track | 13 / 30 / 44 | Curated track, enrol, patterns as syllabus, `pattern_match` questions, decision tree, library | `GET /v1/tracks/catalog`, `POST`/`DELETE /v1/tracks/{id}/enrol`; Track detail lists the 24 patterns (`new` until first answered); `GET /v1/tracks/{id}/library` (when to reach, tells, pseudocode, walkthrough, `animation_id`) and `/decision-tree`; `POST /v1/review-sessions {kind: dsa_patterns, time_budget_minutes, difficulty_mix}` → `pattern_match` items; stage 2 `match` / `question_title` / `leetcode_url` (BE-09) | ready | Animations stay in the front end (`animation_id` keys v1's reels) |

## Sources and reader (FE-05, FE-08)

| Screen | Frames | Needs | Today | Status | Covered by |
|---|---|---|---|---|---|
| Add to Jolt sheet | 52 | Upload N pages to a track as one source (decision A7), with size and hash checks; accepted types and size limit | `POST /v1/uploads/request` with `track_id`, `title`, `pages[]` → one PUT URL per page, `max_bytes_per_file`, `accepted_types`; `confirm` checks each page's size and SHA-256 (BE-05) | ready | — |
| Sources list / table | 14 / 31 / 45 | Title, track, page count, uploaded at, status incl. `failed`, concepts found, questions written, `low_confidence`; filters by track and status | `GET /v1/sources?track_id=&status=` (BE-05) | ready | — |
| Source · Original | 15 | Signed GET URL per page | `GET /v1/sources/{id}` `pages[].url` (BE-05) | ready | — |
| Source · What it found | 16 / 32 / 46 | Active extraction markdown | `GET /v1/sources/{id}` `active_read.markdown`, `concepts` (BE-05) | ready | — |
| Source · Past reads | 17 / 33 / 47 | Every read: model, time, concepts found, questions written | `GET /v1/sources/{id}` `reads[]`: number, model, client label, time, confidence, concepts found, questions written (BE-05) | ready | — |
| Read again sheet | 53 | Queue a re-read with an optional preferred model; the next `jolt_sync` plan lists it | `POST /v1/sources/{id}/reread` (BE-05); the next `jolt_sync` plan lists it in `reread_sources` with `preferred_model` (BE-04 plan), and `jolt_get_unprocessed_sources` hands it out with `supersede: true`; the superseding `jolt_store_extraction` clears the request | ready | — |
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
  → BE-10 (data hygiene). (Account delete, BE-08, does remove them.)
- **Failure is detected, not reported.** `failed` comes from lapsed sync leases
  (`SOURCE_MAX_ATTEMPTS`); an agent can only note "this file is unreadable" in
  `jolt_sync errors=[...]` (recorded on the sync run, BE-04), which does not mark
  the source. A `jolt_report_source_failure` tool would make it immediate. → a
  later sync pass (BE-10 or its own).
- **`preferred_model` is a hint.** Any connected client may answer a read-again
  request; the plan shows the preference.

**BE-06 decisions and follow-ups for the review screens**

- **One question per concept per session**, rotating through the concept's
  active questions. Answering several questions of one concept on one day would
  fold several same-day reviews into FSRS. The FE-02 mock serves every active
  question; the item shape is the same.
- ~~**"Today" is the UTC day** for `done_today` and `upcoming`.~~ BE-08: the
  user's `time_zone` setting (UTC until the app sets it).
- ~~**`time_budget_minutes` and `kind: dsa_patterns`** are accepted and ignored.~~
  BE-09: DSA sessions are drawn by the v1 rules (API.md section 7).
- **The daily cap does not cut DSA sessions** (decision at the BE-07/BE-09 merge).
  A `kind: dsa_patterns` session is bounded by its `time_budget_minutes`, like
  "practise this concept" is an explicit ask, so `capped` is always `false` on it.
  Its answers still count in `done_today`, so towards the cap of the next
  regular session (the cap is "most answers a day"). A mixed session (no `kind`)
  is capped as usual, DSA patterns included.
- **Mastery threshold**: FSRS stability ≥ `MASTERY_STABILITY_DAYS` (21) after a
  Good or Easy answer, the same number as the FE mock. Retirement is evaluated
  when reading (setting on/off takes effect at once, no rewrite of cards).
- **Rewrites happen at grading, not at the result screen.** The result screen
  cannot say "rewritten"; the next session's queue item does (`rewritten`).
  Today → "New since last sync" lists them: each new version is a
  `question_rewritten` change on the sync run (`GET /v1/sync/status`).
- ~~**"Today" is the UTC day** also for D6 upload sessions.~~ BE-08: see 8 below.
- ~~**No daily review cap yet.**~~ BE-08: `daily_review_cap` in settings.

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
- ~~**`enrolled`** is `true` on a curated track where the caller has concepts.~~
  BE-09: real enrolment (`POST`/`DELETE /v1/tracks/{id}/enrol`).

**BE-09 decisions and follow-ups for the DSA track**

- **The pattern is the answer, so it is masked before stage 2.** A
  `pattern_match` queue item carries the **track id** as `concept_id` and
  `"Pattern match"` as `concept_title` (stage 1 takes that id back and finds the
  pattern); the title and the LeetCode link come with stage 2, which also names
  the real `concept_id`. `GET /v1/reviews/{id}` and `/v1/reviews/due` mask the
  same way; concept pages never list a pattern's problems. The FE-02 mock
  serves the pattern's id and name on queue items: FE-10 keys items by
  `question_id` and shows the pattern only from stage 2.
- **Spaced repetition per pattern.** A review of a problem folds into the
  learner's card for its pattern (correct Good, partial Hard, incorrect Again,
  final at once; no semantic grading, never in `jolt_get_pending_gradings`).
  Track detail and the queue therefore show patterns; Today's due count
  includes due patterns (one problem each in the ordinary queue).
- **Draw = v1 rules + priority.** One clock, difficulty mix, no near
  duplicates, as in v1; within a difficulty, due patterns first, then new, then
  learning, a repeat pattern and retired ones last, unseen problems before ones
  answered in the last 14 days. The draw reads the track content's problem
  index (one read) plus the learner's cards on the track and recent reviews.
- **Enrolment lives on the user document** (`enrolments`), not a container:
  `/v1/me`, Tracks and the queue read it with the user at no extra cost, and an
  account delete removes it with the user. Leaving pauses the track (cards and
  answers kept).
- **Track-level content** (decision tree, pattern order, problem index) is one
  document in the `tracks` container's curated bucket (`dsa.content`), without
  `origin`, so the catalog query never returns it (D7: same containers).
- **Library entries live on the pattern concepts**; `animation_id` is the
  pattern's slug when v1 has a reel for it (all 24 do). The animations
  themselves stay front-end data.
- **Options order** is the library's order (the syllabus), the same for every
  problem; v1's grid used the batch's alphabetical tag list (the grid has a
  search, so order is cosmetic).
- **Import reads the TS sources directly** (a strict reader of plain data
  literals: `jolt/curated/ts_literal.py`), so `--from <Jolt repo>` needs no
  Node. `scripts/export_dsa_from_jolt.mjs` (a JSON export with Jolt's esbuild,
  verified to give identical data) is written for Jolt's `npm run export:dsa`;
  `--content` takes its output. → the orchestrator ports it into Jolt.
- **Cards denormalise a pattern's problems** (`active_question_ids`) when they
  are created; a re-import that adds problems does not rewrite every learner's
  card (that would be a cross-partition fan-out). Sessions read the live
  problem index, and Track detail counts problems from it, so only the
  generic queue's rotation for an existing card can lag until the card's next
  refresh. Harmless; noted.
- **Account delete** (BE-08): its purge walks only the user's own tracks
  (`list_for_user`), so curated tracks, concepts, questions and the content
  document survive; the user's enrolments (user document), DSA cards, answers
  and sessions go with the account. Tested since the merge
  (`tests/test_merge_followups.py`), as is the maintenance sweep leaving curated
  content alone.
- **Progress lists curated tracks** the learner is enrolled in (from enrolment,
  before any answer) or has cards on, after their own tracks, with
  `kind: dsa_patterns`.
- **Corrections are sync work** (on BE-04's plan): `pending_corrections` (total
  and per track) counts the concepts `jolt_get_pending_corrections` returns; the
  plan is not `done` while any wait, so a nightly run answers them before it
  closes. Only a changed text is recorded as a `concept_updated` change;
  re-confirming the text clears the notes silently.
- **Reconciliation on the run.** New and successor concepts are `concept_added`
  changes, refined ones `concept_updated`; unchanged and removed ones are not
  shown in "New since last sync" (a removed concept just leaves the queue).
- **A read again stored without `concepts` breaks the chain.** Reconciliation
  compares against the concepts credited to the replaced read; a supersede
  without `concepts` credits none, so the *next* read again sees every concept
  as `new` (duplicates). The prompt, the plan and the tool description all ask
  for `concepts` on a supersede. → a later pass: fall back to the source's
  active concepts when the replaced read has none.

## Progress and settings (FE-09)

| Screen part | Frames | Needs | Today | Status | Covered by |
|---|---|---|---|---|---|
| Progress | 19 / 35 / 49 | Retention and weekly series, reviews, streak, learning / mastered, daily heatmap, per-track table | `GET /v1/progress?weeks=` (BE-08), the FE-02 shape; per-concept `GET /v1/progress/concepts/{id}` | ready | — |
| Settings · Model | 20 / 36 / 50 | MCP status, last sync and health, connected clients (create / revoke tokens), nightly prompt | `model_status` in `GET /v1/settings` (`mcp_server_url`, tokens with `last_used_at`, `last_sync`, `health`, `next_expected_at`; BE-08); create / revoke with `POST` / `DELETE /v1/mcp-tokens` (BE-03); prompt `GET /v1/sync/prompt` (BE-04) | ready | — |
| Settings · API key + sheet | 20 / 36 / 50, 51, 58 | Add, list (`added_at`), remove | `POST`, `GET` (with `added_at`, BE-08), `DELETE /v1/settings/api-key` | ready | — |
| Settings · Appearance | 20 / 36 / 50 | Theme | `theme` in `GET` / `PATCH /v1/settings` (BE-08) | ready | — |
| Settings · Reviews | 20 / 36 / 50 | Desired retention (FSRS), daily cap, retire mastered, allow question rewrite | All four in `GET` / `PATCH /v1/settings`, each with its behaviour: desired retention per user in every FSRS fold, daily cap on session size (BE-08), retire / rewrite (BE-06) | ready | — |
| Settings · Data | 20 / 36 / 50, 59 | Storage used, export, delete account | `GET /v1/storage` (BE-05); `POST /v1/export` → signed download URL, `DELETE /v1/me {confirm: "DELETE"}` (BE-08); `data_retention_days` in settings, enforced by the daily maintenance sweep (BE-10) | ready | — |

**BE-08 decisions and follow-ups for the progress and settings screens**

- **Field names follow the FE-02 shapes** (`Jolt/src/api/types-pending.ts`).
  Additions: `time_zone` in settings, `next_expected_at` in `model_status`,
  `daily_review_cap` / `capped` on review sessions, `added_at` on the API key
  POST response. `allow_question_rewrite` defaults to `false` (D8); the FE mock
  defaults it to `true`.
- **"Active" concepts for retention** = studied and not retired: a retired
  (mastered) concept is never reviewed again, so its predicted recall would only
  decay. The FE mock counts retired concepts as 1.0 instead; with
  `retire_mastered` off they count with their real recall.
- **Learning and mastered are disjoint** (learning = studied, not mastered). The
  mock counts a mastered but not retired concept as learning too.
- **`reviews_this_week` is the calendar week** (since Monday, user's zone), to
  match `retention_weekly`'s Monday weeks; the mock sums the last 7 days.
- **`retention_weekly` leaves out weeks with nothing studied yet**, like the
  mock, so it can have fewer than `weeks` points.
- **Desired retention changes future intervals only.** Cards keep the due date
  they have until their next answer (no bulk reschedule).
- ~~**Data retention is stored, not enforced.**~~ Fixed in BE-10: the
  `jolt-maintenance` Container Apps Job (`python -m jolt.maintenance`, daily)
  deletes sources past the user's `data_retention_days` with their files and
  reads, and uploads never confirmed after 24 h. Concepts, questions and review
  history learned from a deleted source stay; sessions keep its (dangling) id.
- **Export holds documents, not files.** The bundle has every document and
  every read's markdown, with page metadata but not the uploaded images or
  PDFs. Zipping originals would make the export a long-running job. → later, if
  asked for.
- **Delete is synchronous and in-request.** Fine at MVP sizes (one partition
  query per container plus one per track, source, review and concept); a big
  account could hit the request timeout, in which case the retry finishes it
  (idempotent, tokens go last). A nightly sync running during the delete could
  write a document after its container was swept. Blob soft delete keeps
  deleted files 7 days and Cosmos backups 48 h. **Deploy pending**: tested
  against the in-memory fakes, not the Cosmos emulator or Azure.

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
   concepts — BE-09: except Jolt's own `curated` concepts, readable by all and
   writable by none). `tests/test_ownership.py` covers every such tool and route. To be
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
4. ~~**`PUT /v1/settings/retention` naming.**~~ Fixed in BE-08: settings now carry
   `data_retention_days` (uploads, in days) and `desired_retention` (FSRS, 0.80–0.97)
   side by side; the PUT is kept as a deprecated alias of `data_retention_days`, and
   documents with the old top-level `retention_days` are migrated on read.
5. ~~**Token lookup scans every user** (Argon2 verify per user).~~ Fixed in
   BE-03: an indexed HMAC lookup key, then one Argon2 verify.
6. **`GET /v1/reviews/due` `limit`** counts cards, not questions; a card with
   several active questions returns them all. Kept as it is for compatibility
   (it now leaves out retired concepts); review sessions (BE-06) serve one
   question per concept and are what the app uses.
7. **Tokens never expire, and app tokens cannot be listed.** Sign-out and MCP
   token revocation work, but a lost device's app token stays valid until the
   pepper rotates, and Settings has no "signed-in devices" list. → BE-10
   (expiry / idle timeout for app tokens, optional device list).
8. ~~**Upload dates and the schedule have no user time zone.**~~ Fixed in BE-08:
   a `time_zone` setting (IANA, default `UTC`) decides the D6 upload date, the
   plan's `upload_dates`, "today" (done today, daily cap), the session summary's
   next days, progress days and weeks, and the default `tz` of
   `GET /v1/sync/status`. The app should set it from the device after sign-in;
   until then everything stays UTC as before. Upload dates of sessions already
   logged are not rewritten.
9. **No Streamable HTTP transport.** `/mcp` speaks HTTP+SSE only (mcp 1.2.0), so
   Codex connects through the `mcp-remote` bridge instead of its native `url =`
   config, and every sync holds one long event stream (to check against the
   Container Apps ingress timeout after deploy). → upgrade `mcp` (≥ 1.8, moves
   Pydantic to 2.11+) and add Streamable HTTP at `/mcp`; BE-10 or its own pass.
   See `CONNECT.md`.
10. **`jolt_create_questions` accepts any number of options.** The prompt and
    the plan ask for exactly 4 with one correct; the server only checks the
    request shape, and a `rewritten_question` (BE-06) needs only two or more.
    Not done in BE-07 (outside its brief). → a later pass: enforce 4 in both.
