# Nightly sync prompt

The text a learner pastes into a **scheduled task** in Claude or Codex so that,
every night, their own model reads the day's uploads, writes concepts and
questions, and grades their written answers in Jolt. Schedule it **daily at
00:15 local time** (decision D5). A sync works through everything that is
waiting, so a missed night is caught up the next one.

- The app shows and copies it from `GET /v1/sync/prompt` (`{prompt,
  schedule_time, version}`): the served `prompt` is exactly the text between the
  `prompt:start` and `prompt:end` markers below, with `{{RUBRIC_VERSION}}`
  replaced by the current grading-rubric version.
- Bump `prompt-version` whenever the prompt text changes.
- The model needs the Jolt MCP server connected first: see
  [`CONNECT.md`](./CONNECT.md).

<!-- prompt-version: 2026-10-26.1 -->

<!-- prompt:start -->
You are running my nightly Jolt sync. Jolt is my spaced-repetition app and is
connected to you as the "jolt" MCP server. I am asleep: do not ask me anything.
When something is unclear, make the most reasonable choice and carry on.

## The loop

1. Call `jolt_sync`. It returns a plan: what is waiting, the tracks involved,
   and ordered `steps` (tool, purpose, limit).
2. If `done` is true, stop.
3. Otherwise follow the steps in order. Never send more items in one call than
   the step's `limit`. Finish the sources in `claimed_sources` before claiming
   more.
4. Call `jolt_sync` again. Pass `errors` with a short line for anything you
   could not do (for example `"source 3f2a…: pages 2-3 unreadable"`). Follow the
   new plan. Repeat until `done` is true.

Always end with a `jolt_sync` that returns `done: true`; that is what records
the night's sync in the app. If a tool call fails, retry it once; if it fails
again, skip that item and report it in `errors` at the next `jolt_sync`.

## Reading a source

- `jolt_get_source_content` gives a short-lived `read_url`. Fetch it straight
  away (call again for a fresh one if it expired) and read every page.
- Transcribe faithfully, handwriting included. Keep the structure (headings,
  lists, formulas, diagrams described in words). Mark what you cannot read as
  `[illegible]` instead of guessing.
- `jolt_store_extraction`: the full markdown, an honest `confidence` from 0 to 1
  (below 0.6 when you are unsure of the handwriting or content), and your model
  name as `model_id`.

## Writing concepts (`jolt_log_session`)

- One idea per concept. `text` is the idea itself in one to three plain
  sentences, self-contained, in the words of my notes: what I should remember,
  not "the notes say...".
- `title`: two to six words.
- `concept_key`: a short lowercase-hyphenated key (`consistent-hashing`). Reuse
  a key from the track's agenda when the concept belongs to it (call
  `jolt_get_agenda` once per track); keep keys stable.
- `track_id`: the source's track. For sources without a track, pick the best
  fit from the plan's `tracks`.
- `source_id`: always set it to the source the concept came from.
- Follow each track's `guidance` in the plan: refine a draft agenda with
  `jolt_set_agenda` (`mark_refined: true`, keep existing keys); never add new
  keys to a locked one.
- Before logging, check `jolt_get_recent_concepts` for the track and skip
  concepts that already exist. Skip trivia, page numbers and headings with no
  content. Two to eight concepts per page is typical.

## Writing questions (`jolt_create_questions`)

Write one to three questions for every new concept:

- `stem`: a free-text prompt that makes me recall the idea in my own words
  (why, how, what happens if, compare). Answerable from the concept alone.
  Never a yes/no question, and never give the answer away in the stem.
- `expected_answer`: a complete model answer in one to three sentences. My
  written answer is graded against it, so include every point that matters.
- `options`: exactly 4. One is correct (a short paraphrase of the expected
  answer); the other three are plausible, similar in length and style, and
  based on real misconceptions. No "all of the above" or joke options.
- `correct_index`: the 0-based position of the correct option. Vary it between
  questions.

## Grading my answers (`jolt_get_pending_gradings`, `jolt_submit_gradings`)

- Compare my `free_text` with the `expected_answer` on substance, not wording
  or spelling.
- `suggested_fsrs_grade`: 1 Again (wrong, missing or contradicting), 2 Hard
  (core idea present but partial), 3 Good (correct with minor gaps), 4 Easy
  (complete and precise). When torn between two grades, choose the lower. An
  empty answer or "I don't know" is 1.
- `rationale`: one sentence addressed to me saying what was right or missing.
- `rubric_version`: `{{RUBRIC_VERSION}}`.

## Boundaries

- Use only the `jolt_*` tools for Jolt data. Do not upload files and do not
  edit existing concepts unless one is clearly wrong (`jolt_correct_concept`).
- When you finish, reply with one line: sources read, concepts added,
  questions written, answers graded, and any problems.
<!-- prompt:end -->

## Scheduling it

| Client | Where | Schedule |
|---|---|---|
| Claude Desktop | A scheduled task in the Claude desktop app, with the Jolt connector enabled for it | Daily, 00:15 |
| Codex | A scheduled automation in the Codex app, with the `jolt` MCP server from `~/.codex/config.toml` | Daily, 00:15 |

The computer must be awake and the client signed in at 00:15 for a local
scheduled task to fire. If it is not, the next run catches up, and the app shows
the sync as `late` after 30 hours without one.

**Deploy pending:** the exact menu names for creating a scheduled task in each
client are to be confirmed against the real apps after the `dev` deploy (see
`CONNECT.md`, "To confirm after deploy").
