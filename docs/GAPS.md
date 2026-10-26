# Screen → endpoint gaps

Each screen in the Jolt 2.0 design (Figma prototype, frames listed in
`Jolt/design/frames/index.json`; screens per pass in `Jolt/docs/v2/PLAN.md` §5 and
the FE pass briefs) compared with the API as of BE-08 ([`API.md`](./API.md)). For
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
| First track | 4 / 21 | Create a notes track with name and optional outline (→ `draft`); or enrol in curated DSA | `POST /v1/tracks {name, syllabus}` gives a `refined` agenda, no outline/`draft`, no `kind` | partial | BE-07 (outline → `draft`, `kind`), BE-09 (catalog, enrol) |
| Waiting for first sync | — / 21 | Poll sync status; resume point for a returning user | `GET /v1/sync/status` (`health: never` until the first run), `GET /v1/me` `onboarding.first_sync_at` (set when the first run closes) | ready | — |

## Today (FE-04)

| Screen part | Frames | Needs | Today | Status | Covered by |
|---|---|---|---|---|---|
| Due card | 5 / 22 / 37 | Due count and per-track breakdown | `POST /v1/review-sessions` items carry `track_id`, `track_name` (BE-06); a per-track count without starting a session is still missing | partial | BE-07 (due per track in `GET /v1/tracks`) |
| Sync line | 5 / 22 / 37 | Last sync time, client, late / never | `GET /v1/sync/status`: `last_run` (`started_at`, `finished_at`, `client_label`, counts), `health`, `next_expected_at` (pass `?tz=`) | ready | — |
| Stats | 5 / 22 / 37 | Streak, retention, reviews this week | `GET /v1/progress?weeks=1`: `streak_days`, `retention`, `reviews_this_week` (BE-08) | ready | — |
| Tracks | 5 / 22 / 37 | Concepts, due and sources count per track | `GET /v1/tracks` has no counts | partial | BE-07 |
| Tonight's uploads | 5 / 22 / 37 | Unprocessed sources with their track | `GET /v1/sources?status=unprocessed` with `track_id`, `track_name`, `title`, `page_count` (BE-05) | ready | — |
| What's new | 5 / 22 / 37 | Concepts added by the last sync, with the source date ("Found in your 12 Sep notes") | `changes[]` on each sync run (`GET /v1/sync/status` → `last_run`, `GET /v1/sync/runs`): `concept_added` / `concept_updated` with `concept_title`, `track_id`, `source_id`, `source_uploaded_at`, `added_to_syllabus` (decision A25, BE-04) | ready | — |
| Empty / first-run states | — | Same data as above, plus `GET /v1/me` | `GET /v1/me` (BE-03), sync status (BE-04), `GET /v1/sources` (BE-05), stats (BE-08); due and track counts still partial (rows above) | partial | BE-07 |

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
| Tracks | 10 / 27 / 41 | List with counts, `kind`, curated badge | `GET /v1/tracks` (agenda, origin; no counts, no kind) | partial | BE-07, BE-09 (`kind`) |
| New track sheet | 10 / 27 / 41 | Name and optional outline → `draft` agenda | `POST /v1/tracks` (labels → `refined`) | partial | BE-07 |
| Track detail | 11 / 28 / 42 | One call: agenda items with state (new / learning / due / mastered / not covered), next review, question count; due today; recent sources | Agenda via `GET /v1/tracks`; coverage via `GET /v1/tracks/{id}/coverage`; no per-item state, no `GET /v1/tracks/{id}` | partial | BE-07 |
| Syllabus mode pill and overlay | 11 / 28 / 42, 54 | `agenda.status` | In every `Track` | ready | — |
| Lock toggle | 11 / 28 / 42 | Lock / unlock | `POST /v1/tracks/{id}/agenda/lock` and `/unlock` | ready | — |
| "Added to the syllabus from your notes" | 11 / 28 / 42 | Per-item provenance (auto-added vs authored) | Only track-level `agenda.source` | partial | BE-07 (not named in the brief; add a per-item `source`) |
| Concept | 12 / 29 / 43 | Summary, source and page, answer timeline (date, your words, correct, grade, feedback, question version), adaptive question note, FSRS state | `GET /v1/progress/concepts/{id}`: FSRS state and timeline with grade, correct, `grading`, `feedback`, `question_version` (BE-06); `GET /v1/concepts/{id}/questions`: version chain with `rewrite_reason` (BE-06); `GET /v1/reviews/{id}` has the learner's words; `POST /v1/concepts/{id}/unretire` (BE-06). No summary, source, words in the timeline, or `retired` flag in one call | partial | BE-07 (`GET /v1/concepts/{id}` folds these together) |
| Feedback panel gate | 12 / 29 / 43 | `has_api_key` | `has_api_key` on `GET /v1/me` (added in BE-03 for BE-08) | ready | panel content BE-11 |
| "Something's wrong with this concept" | 12 / 29 / 43 | Store a correction note for the next sync | Agents can call `jolt_correct_concept`; no REST route | missing | BE-07 (`POST /v1/concepts/{id}/correction`) |
| DSA track (placeholder) | 13 / 30 / 44 | Curated track, enrol, patterns as syllabus, `pattern_match` questions, decision tree, library | — | missing | BE-09 |

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
- **`time_budget_minutes` and `kind: dsa_patterns`** are accepted; DSA sessions
  are empty until BE-09 adds DSA tracks and `pattern_match` questions (`match`,
  `question_title`, `pattern_match` are `null` until then).
- **Mastery threshold**: FSRS stability ≥ `MASTERY_STABILITY_DAYS` (21) after a
  Good or Easy answer, the same number as the FE mock. Retirement is evaluated
  when reading (setting on/off takes effect at once, no rewrite of cards).
- **Rewrites happen at grading, not at the result screen.** The result screen
  cannot say "rewritten"; the next session's queue item does (`rewritten`).
  Today → "New since last sync" lists them: each new version is a
  `question_rewritten` change on the sync run (`GET /v1/sync/status`).
- ~~**"Today" is the UTC day** also for D6 upload sessions.~~ BE-08: see 8 below.
- ~~**No daily review cap yet.**~~ BE-08: `daily_review_cap` in settings.

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

1. **Ownership checks on track- and concept-keyed calls.** Concepts and questions
   are partitioned by track and concept, not by user, and these paths check only
   that the caller has *a* valid token, not that the track or concept is theirs:
   `jolt_get_recent_concepts`, `jolt_diff_extractions`, `jolt_correct_concept`,
   `jolt_create_questions`, `jolt_log_session` (any `track_id`). (Stage 1 now
   requires the caller's card for the concept: fixed in BE-06; the new concept
   routes check the same.) IDs are random
   32-hex values, so this is not exploitable by guessing, but it breaks the
   LLD §5 scope rule. → BE-07 (it reworks the concept read models), confirmed in the
   BE-10 security review.
2. **Supersede does not reconcile concepts.** `jolt_store_extraction(supersede=true)`
   only marks the previous extraction `superseded`. The tool description promises
   concept reconciliation (refined / changed / removed / new), which runs only
   as a preview in `jolt_diff_extractions`. Until it lands, every concept a read
   logs shows `is_new: true` in Past reads (BE-05). → BE-07 (its reconciliation-ceiling item).
3. **`jolt_correct_concept` description vs behaviour.** The description says a
   missing concept gives `updated: false`; the tool returns
   `{error: "concept not found"}`. The output schema in the contract describes
   what the code does. → fix the description in BE-07.
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
    → BE-07 or a later pass: enforce 4 in both.
