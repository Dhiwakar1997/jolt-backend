# Screen → endpoint gaps

Each screen in the Jolt 2.0 design (Figma prototype, frames listed in
`Jolt/design/frames/index.json`; screens per pass in `Jolt/docs/v2/PLAN.md` §5 and
the FE pass briefs) compared with the API as of BE-04 ([`API.md`](./API.md)). For
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
| Due card | 5 / 22 / 37 | Due count and per-track breakdown | `GET /v1/reviews/due` gives a count and items, but items carry no `track_id` | partial | BE-06 (review sessions), BE-07 (due per track in `GET /v1/tracks`) |
| Sync line | 5 / 22 / 37 | Last sync time, client, late / never | `GET /v1/sync/status`: `last_run` (`started_at`, `finished_at`, `client_label`, counts), `health`, `next_expected_at` (pass `?tz=`) | ready | — |
| Stats | 5 / 22 / 37 | Streak, retention, reviews this week | — | missing | BE-08 (`GET /v1/progress`) |
| Tracks | 5 / 22 / 37 | Concepts, due and sources count per track | `GET /v1/tracks` has no counts | partial | BE-07 |
| Tonight's uploads | 5 / 22 / 37 | Unprocessed sources with their track | `GET /v1/sources` (filter on the client; no `track_id`) | partial | BE-05 |
| What's new | 5 / 22 / 37 | Concepts added by the last sync, with the source date ("Found in your 12 Sep notes") | `changes[]` on each sync run (`GET /v1/sync/status` → `last_run`, `GET /v1/sync/runs`): `concept_added` / `concept_updated` with `concept_title`, `track_id`, `source_id`, `source_uploaded_at`, `added_to_syllabus` (decision A25, BE-04) | ready | — |
| Empty / first-run states | — | Same data as above, plus `GET /v1/me` | `GET /v1/me` (BE-03), sync status (BE-04); due, stats and track counts still partial (rows above) | partial | BE-05, BE-07, BE-08 |

## Review (FE-06)

| Screen | Frames | Needs | Today | Status | Covered by |
|---|---|---|---|---|---|
| Queue / session start | — | Ordered queue interleaving tracks, most overdue first | `GET /v1/reviews/due` (no session, no interleaving) | partial | BE-06 (`POST /v1/review-sessions`) |
| Review 1 · Your own words | 6 / 23 / 38 | Stage 1 locks the text, then releases options; idempotent on a client request id | `POST /v1/reviews/{question_id}/stage1` (not idempotent) | partial | BE-06 |
| Review 2 · Pick the best match | 7 / 24 / 39 | Options only after stage 1 | Returned by stage 1 | ready | — |
| Review 3 · Result | 8 / 25 / 40 | Correct, expected answer, next due date, provisional grade, "question rewritten" note | `POST /v1/reviews/{review_id}/stage2` has no provisional grade, no version note, no idempotency | partial | BE-06 |
| Review 4 · Session complete | 9 / 26 | Summary with "what moved" (old and new due dates) | — | missing | BE-06 (`POST /v1/review-sessions/{id}/end`) |

## Tracks and concepts (FE-07)

| Screen | Frames | Needs | Today | Status | Covered by |
|---|---|---|---|---|---|
| Tracks | 10 / 27 / 41 | List with counts, `kind`, curated badge | `GET /v1/tracks` (agenda, origin; no counts, no kind) | partial | BE-07, BE-09 (`kind`) |
| New track sheet | 10 / 27 / 41 | Name and optional outline → `draft` agenda | `POST /v1/tracks` (labels → `refined`) | partial | BE-07 |
| Track detail | 11 / 28 / 42 | One call: agenda items with state (new / learning / due / mastered / not covered), next review, question count; due today; recent sources | Agenda via `GET /v1/tracks`; coverage via `GET /v1/tracks/{id}/coverage`; no per-item state, no `GET /v1/tracks/{id}` | partial | BE-07 |
| Syllabus mode pill and overlay | 11 / 28 / 42, 54 | `agenda.status` | In every `Track` | ready | — |
| Lock toggle | 11 / 28 / 42 | Lock / unlock | `POST /v1/tracks/{id}/agenda/lock` and `/unlock` | ready | — |
| "Added to the syllabus from your notes" | 11 / 28 / 42 | Per-item provenance (auto-added vs authored) | Only track-level `agenda.source` | partial | BE-07 (not named in the brief; add a per-item `source`) |
| Concept | 12 / 29 / 43 | Summary, source and page, answer timeline (date, your words, correct, grade, feedback, question version), adaptive question note, FSRS state | `GET /v1/progress/concepts/{id}`: FSRS state and timeline of date, grade, correct; no text, no words, no feedback, no versions, no source | partial | BE-07 (`GET /v1/concepts/{id}`), BE-06 (question versions, feedback summary) |
| Feedback panel gate | 12 / 29 / 43 | `has_api_key` | `has_api_key` on `GET /v1/me` (added in BE-03 for BE-08) | ready | panel content BE-11 |
| "Something's wrong with this concept" | 12 / 29 / 43 | Store a correction note for the next sync | Agents can call `jolt_correct_concept`; no REST route | missing | BE-07 (`POST /v1/concepts/{id}/correction`) |
| DSA track (placeholder) | 13 / 30 / 44 | Curated track, enrol, patterns as syllabus, `pattern_match` questions, decision tree, library | — | missing | BE-09 |

## Sources and reader (FE-05, FE-08)

| Screen | Frames | Needs | Today | Status | Covered by |
|---|---|---|---|---|---|
| Add to Jolt sheet | 52 | Upload N pages to a track as one source (decision A7), with size and hash checks; accepted types and size limit | `POST /v1/uploads/request` + `confirm`: one file per source, no `track_id`, no size, no type allow-list | partial | BE-05 |
| Sources list / table | 14 / 31 / 45 | Title, track, page count, uploaded at, status incl. `failed`, concepts found, questions written, `low_confidence`; filters by track and status | `GET /v1/sources`: filename, status (no `failed`), active extraction id, created at | partial | BE-05 |
| Source · Original | 15 | Signed GET URL per page | Only via MCP `jolt_get_source_content`; no REST route | missing | BE-05 (`GET /v1/sources/{id}`) |
| Source · What it found | 16 / 32 / 46 | Active extraction markdown | `GET /v1/sources/{id}/extractions` omits the markdown | missing | BE-05 |
| Source · Past reads | 17 / 33 / 47 | Every read: model, time, concepts found, questions written | `GET /v1/sources/{id}/extractions`: model, confidence, time, status; no counts | partial | BE-05 |
| Read again sheet | 53 | Queue a re-read with an optional preferred model; the next `jolt_sync` plan lists it | The plan side is built (BE-04): `reread_sources` and re-read steps appear once `SourcesRepository.list_reread_requested(user_id)` exists; no way to request one yet | partial | BE-05 (`POST /v1/sources/{id}/reread`, `list_reread_requested`, clearing the flag on the superseding `jolt_store_extraction`) |
| Reader | 18 / 34 / 48 | Page URLs; screen-size images first, full size on zoom; thumbnails on web | — | missing | BE-05 (page URLs). **Resized renditions and thumbnails are in no brief**: BE-05 should decide (originals only, or renditions) |
| Toasts | 56, 57 | — | — | client | — |

## Progress and settings (FE-09)

| Screen part | Frames | Needs | Today | Status | Covered by |
|---|---|---|---|---|---|
| Progress | 19 / 35 / 49 | Retention and weekly series, reviews, streak, learning / mastered, daily heatmap, per-track table | Only per-concept `GET /v1/progress/concepts/{id}` | missing | BE-08 (`GET /v1/progress`) |
| Settings · Model | 20 / 36 / 50 | MCP status, last sync and health, connected clients (create / revoke tokens), nightly prompt | `GET`/`POST /v1/mcp-tokens`, `DELETE /v1/mcp-tokens/{id}` with `label`, `last_used_at` (BE-03); `GET /v1/sync/status`, `GET /v1/sync/runs`, `GET /v1/sync/prompt` (BE-04) | partial | BE-08 (`model_status` block in `GET /v1/settings`, which can reuse these) |
| Settings · API key + sheet | 20 / 36 / 50, 51, 58 | Add, list (`added_at`), remove | `POST`, `GET` (no `added_at`), `DELETE /v1/settings/api-key` | partial | BE-08 |
| Settings · Appearance | 20 / 36 / 50 | Theme | — | missing | BE-08 (`GET/PATCH /v1/settings`) |
| Settings · Reviews | 20 / 36 / 50 | Desired retention (FSRS), daily cap, retire mastered, allow question rewrite | — (`FSRS_DESIRED_RETENTION` is a server-wide setting) | missing | BE-08 (settings), BE-06 (behaviour) |
| Settings · Data | 20 / 36 / 50, 59 | Storage used, export, delete account | `PUT /v1/settings/retention` stores a data-retention preference in days that nothing uses yet | missing | BE-05 (storage figure), BE-08 (export, `DELETE /v1/me`) |

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
   `jolt_create_questions`, `jolt_log_session` (any `track_id`), and
   `POST /v1/reviews/{question_id}/stage1` (any `concept_id`). IDs are random
   32-hex values, so this is not exploitable by guessing, but it breaks the
   LLD §5 scope rule. → BE-07 (it reworks the concept read models), confirmed in the
   BE-10 security review.
2. **Supersede does not reconcile concepts.** `jolt_store_extraction(supersede=true)`
   only marks the previous extraction `superseded`. The tool description promises
   concept reconciliation (refined / changed / removed / new), which runs only
   as a preview in `jolt_diff_extractions`. → BE-07 (its reconciliation-ceiling item).
3. **`jolt_correct_concept` description vs behaviour.** The description says a
   missing concept gives `updated: false`; the tool returns
   `{error: "concept not found"}`. The output schema in the contract describes
   what the code does. → fix the description in BE-07.
4. **`PUT /v1/settings/retention` naming.** It stores *data* retention in days,
   which is easy to confuse with FSRS *desired retention* (Settings · Reviews). →
   BE-08 should fold it into `PATCH /v1/settings` with explicit names.
5. ~~**Token lookup scans every user** (Argon2 verify per user).~~ Fixed in
   BE-03: an indexed HMAC lookup key, then one Argon2 verify.
6. **`GET /v1/reviews/due` `limit`** counts cards, not questions; a card with
   several active questions returns them all. → BE-06 replaces it with review
   sessions.
7. **Tokens never expire, and app tokens cannot be listed.** Sign-out and MCP
   token revocation work, but a lost device's app token stays valid until the
   pepper rotates, and Settings has no "signed-in devices" list. → BE-10
   (expiry / idle timeout for app tokens, optional device list).
8. **Upload dates and the schedule have no user time zone.** Sessions are split
   by the UTC date of each upload (D6), so a learner west of UTC who uploads late
   in the evening sees that upload under the next day; `GET /v1/sync/status`
   takes the zone per request (`?tz=`) for `next_expected_at`. → BE-08: a
   `timezone` setting, used for the D6 day and as the default `tz`.
9. **No Streamable HTTP transport.** `/mcp` speaks HTTP+SSE only (mcp 1.2.0), so
   Codex connects through the `mcp-remote` bridge instead of its native `url =`
   config, and every sync holds one long event stream (to check against the
   Container Apps ingress timeout after deploy). → upgrade `mcp` (≥ 1.8, moves
   Pydantic to 2.11+) and add Streamable HTTP at `/mcp`; BE-10 or its own pass.
   See `CONNECT.md`.
10. **`jolt_create_questions` accepts any number of options.** The prompt and
    the plan ask for exactly 4 with one correct; the server only checks the
    request shape. → BE-06 (question versions) can enforce 4 when it reworks
    questions.
