# Jolt — Low-Level Design

**Version:** 0.2 (draft)
**Date:** 30 August 2026
**Companion to:** Jolt Functional Specification v0.6
**Status:** For review

---

## 1. Scope and stance

This document turns the functional spec into a buildable backend design: framework choices, service topology, module boundaries, the data-access layer, the auth model, and the request lifecycles for every meaningful operation.

Two principles from the spec constrain most decisions here:

1. **Jolt runs no inference in the core loop.** Storage, scheduling, retrieval, and pure computation only — the entire free tier originates zero LLM calls from Jolt's compute. The one deliberate exception is the optional key-backed path (§5.1): when a user has stored an API key, Jolt may make model calls *on that user's behalf and billed to their key* for on-demand features. This is a bounded second mode, not the default.
2. **The MCP tool surface is the product.** The server is thin, but the contract it presents to agents is precise, versioned, and defensive.

The consequence worth stating up front: because the core loop does no inference, there is **no extraction worker queue**. "Processing an uploaded file" means "an agent pulls it on the user's next sync." The only always-on timed job Jolt runs is FSRS recomputation.

---

## 2. Technology stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12 | Chosen. Mature MCP SDK, strong Azure SDKs, `fsrs` reference lib |
| Web framework | FastAPI | Async-native, Pydantic validation, auto OpenAPI for the REST side |
| MCP | Official Python MCP SDK, HTTP+SSE transport, mounted in FastAPI | Remote server reachable by networked agents and scheduled tasks |
| ASGI server | Uvicorn (behind Container Apps ingress) | Standard async server |
| Data | Azure Cosmos DB, serverless, NoSQL API, `azure-cosmos` async SDK | Spec decision; no ORM, Pydantic ↔ document directly |
| Files | Azure Blob Storage, Hot tier, `azure-storage-blob` async SDK | Source files and, later, Read My Book PDFs |
| Scheduler | Container Apps Jobs (cron-triggered) | FSRS recompute; no always-on worker |
| Email | Deferred (post-MVP) | Nudges cut from MVP; users open the app to see due tasks |
| Secrets | Azure Key Vault | Signing secrets, and per-user provider API keys (§5.1) |
| FSRS | `fsrs` (open-source py-fsrs) | In-process, pure computation |
| Model calls (key-backed) | `httpx` async client, provider-agnostic adapter | Only on the optional key path; billed to user's key |
| Auth | Per-user opaque tokens issued by Jolt | See §5 |

### Why FastAPI over the alternatives

Django brings an ORM and admin that fight Cosmos' schema-flexibility and add weight Jolt does not need. Flask is synchronous by default, and this workload is I/O-bound on Cosmos and Blob — async matters. FastAPI's Pydantic models also double as the request/response contracts for the REST API the Flutter app consumes, so validation and schema are one artifact, not two.

### Why one service now

The MCP server and the REST API share the same data-access layer, the same auth, the same models. At launch they run as one FastAPI app on one Container App, mounted at different route prefixes. The split point later is clean: the MCP server is read-heavy and bursty (syncs), the REST API is write-light and steady (uploads, review submissions). When their scaling profiles diverge enough to matter, they separate along the module boundary already drawn in §4 — no rewrite, just two deployments of the same codebase with different entrypoints.

---

## 3. Service topology

```
                    Azure Container Apps Environment
┌──────────────────────────────────────────────────────────────┐
│                                                                │
│   ┌────────────────────────────────────────────┐              │
│   │  jolt-api  (FastAPI, Uvicorn)               │  scale 0..N  │
│   │                                              │              │
│   │   /mcp/*    → MCP SSE endpoint (agents)      │              │
│   │   /v1/*     → REST API (Flutter app)         │              │
│   │   /health   → liveness                       │              │
│   └───────────┬───────────────────┬──────────────┘             │
│               │                   │                            │
│               │          ┌────────▼──────────┐                 │
│               │          │ jolt-job-fsrs     │  cron job       │
│               │          │ (periodic recompute)│ scale-on-cron │
│               │          └────────┬──────────┘                 │
│               │                   │                            │
└───────────────┼───────────────────┼────────────────────────────┘
                │                   │
        ┌───────▼───────┐   ┌───────▼───────┐   ┌──────────────┐
        │  Cosmos DB    │   │  Blob Storage │   │  Key Vault   │
        │  (serverless) │   │  (Hot LRS)    │   │  (tokens +   │
        │               │   │               │   │   user keys) │
        └───────────────┘   └───────────────┘   └──────────────┘
```

Two deployable units for MVP, one codebase:

- **`jolt-api`** — the FastAPI app, scale-to-zero, serves MCP and REST.
- **`jolt-job-fsrs`** — Container Apps Job, recomputes FSRS state after grades change and refreshes `due_at`, and resets expired leases.

Email nudges (`jolt-job-nudge`) are **cut from MVP**. Users open the app and the due-review query shows pending tasks directly; no timed push, no ACS dependency, no per-timezone fan-out. The job re-enters the topology unchanged if nudges are added back later — the due query it would run already exists for the in-app view.

The FSRS job is separate from the API because it must run on a timer regardless of traffic. It imports the same modules; it is an entrypoint, not a service.

---

## 4. Module structure

One repository, layered so the eventual service split is a packaging change, not a refactor.

```
jolt/
├── main.py                  # FastAPI app assembly, lifespan, mounts
├── config.py                # settings from env (pydantic-settings)
│
├── mcp/                     # MCP tool surface — the product contract
│   ├── server.py            # MCP SDK server, mounted at /mcp
│   ├── tools/
│   │   ├── read.py          # get_tracks, get_recent_concepts, get_coverage
│   │   ├── write.py         # request_upload_url, log_session,
│   │   │                    #   store_extraction, create_questions,
│   │   │                    #   correct_concept
│   │   ├── sync.py          # get_unprocessed_sources, get_pending_gradings,
│   │   │                    #   submit_gradings, sync
│   │   └── extract.py       # list_sources, get_source_content,
│   │                        #   diff_extractions, store_extraction(supersede)
│   └── descriptions/        # tool descriptions + versioned grading rubric
│                            #   (treated as product assets, reviewed on change)
│
├── api/                     # REST for the Flutter app
│   ├── routes/
│   │   ├── upload.py        # request upload URL, confirm upload
│   │   ├── review.py        # fetch due, submit stage-1, submit stage-2
│   │   ├── tracks.py        # list/create tracks, coverage, decay
│   │   ├── sources.py       # source list, extraction history, promote run
│   │   ├── progress.py      # understanding-over-time, concept detail
│   │   └── settings.py      # notification prefs, retention, api key
│   └── deps.py              # FastAPI dependencies (auth, user context)
│
├── domain/                  # business logic, no framework imports
│   ├── models.py            # Pydantic models = spec §5 data model
│   ├── scheduling.py        # FSRS wrapper, grade synthesis, reschedule
│   ├── reconciliation.py    # spec §8 supersede/diff/classify
│   ├── grading.py           # provisional grade rules, lease management
│   └── coverage.py          # syllabus ↔ concept coverage computation
│
├── data/                    # data-access layer, the only thing touching Azure
│   ├── cosmos.py            # container clients, partition-key discipline
│   ├── blob.py              # signed URL generation, upload/read
│   ├── repositories/        # one per aggregate: tracks, sources, concepts,
│   │                        #   reviews, extractions, users
│   └── leases.py            # in_flight lease acquire/release/expire
│
├── auth/
│   ├── tokens.py            # issue, hash, verify opaque tokens
│   └── context.py           # resolve token → user, scope enforcement
│
├── credentials/             # per-user provider API keys (§5.1)
│   ├── vault.py             # store/retrieve/delete keys in Key Vault
│   ├── providers.py         # provider-agnostic adapter (Anthropic/OpenAI/…)
│   └── inference.py         # key-backed model calls for on-demand features
│                            #   (no callers in MVP; capture + storage only)
│
└── jobs/
    └── fsrs_recompute.py    # entrypoint for jolt-job-fsrs
```

The dependency rule: `api/` and `mcp/` depend on `domain/`, `domain/` depends on `data/`, nothing depends upward. `domain/` imports no FastAPI and no Azure SDK, so it is unit-testable without either. This is what makes the module boundary real rather than cosmetic.

`credentials/` is built for MVP but only its capture and storage paths (`vault.py`, `providers.py` validation) have callers; `inference.py` exists as the seam for later features and ships dormant.

---

## 5. Authentication and multi-tenancy

This is the load-bearing design decision. When a scheduled task fires `jolt_sync` at 3am with no human present, the server must know whose data to touch, and must not be able to touch anyone else's.

### Token model

- On account creation, Jolt issues a **per-user opaque token** (`jolt_live_<random>`). Random, not a JWT — there is no need for self-contained claims, and opaque tokens are revocable.
- Jolt stores only a **hash** of the token (Argon2). The plaintext is shown once, at issue time.
- The user places the token in their MCP client config (the connection header) and, for the Flutter app, it is held in secure storage and sent as `Authorization: Bearer`.
- Every MCP tool call and every REST request resolves the token → `user_id` in a FastAPI dependency before any handler runs.

### Why opaque, not OAuth

The spec settled that subscription OAuth cannot route through a third party. Jolt is not federating identity with Anthropic or OpenAI — it is authenticating *its own* users to *its own* server. An opaque token the user pastes into their MCP config is the simplest correct model, and revocation is a single hash deletion.

### Scope enforcement

`user_id` from the token is injected into every repository call as a mandatory argument, and every Cosmos query is partitioned by it (see §6). There is no code path that reads a document without a `user_id` filter except the curated-track read path, which is explicitly global and read-only. A repository method that forgets the filter fails a unit test that asserts partition-key presence.

### The curated-track exception

Jolt tracks (`origin: 'jolt'`) have `user_id = null` and are world-readable. Their concept *definitions* are shared; a user's *progress* against them lives in `concept_states` keyed by that user. So the global read is only ever on immutable curated content, never on anyone's state.

### 5.1 Per-user provider API keys

Built for MVP as capture + secure storage; the features that consume the key come later. The key is **provider-agnostic** — the user picks Anthropic, OpenAI, or another supported provider — and stored **server-side** so it can back either a server-side call from Jolt or be handed to the user's own client flow.

**Capture.** A REST endpoint, `POST /v1/settings/api-key { provider, key }`, over TLS only. The handler validates the key with a cheap provider probe (a minimal models-list call), never a billed inference call, then stores it. A malformed or unauthorised key is rejected at capture rather than discovered at first use.

**Storage.** The raw key goes into **Key Vault** as a secret named by `user_id`+`provider`. Cosmos holds only a reference and metadata: `{ provider, key_ref, last4, added_at, last_validated_at }`. The plaintext never lands in Cosmos, never in logs, never in a response body. `last4` is for the UI ("sk-…4f2a") so the user can recognise which key is stored without it being retrievable.

**Retrieval.** Only `credentials/inference.py` reads the plaintext, only at the moment of a key-backed call, via Managed Identity to Key Vault. The value is held in memory for the call and discarded — never cached to disk, never attached to a log span.

**Deletion.** `DELETE /v1/settings/api-key/{provider}` purges the Key Vault secret and the Cosmos reference in that order, so a crash leaves no dangling reference to a missing secret. This is a real delete, not a soft flag, because the spec's data-controller obligations extend to credentials.

**Provider adapter.** `credentials/providers.py` normalises the differences between providers behind one interface — validate, and (later) call. Adding a provider is a new adapter, not a change to callers. MVP ships the adapter interface and validation for at least Anthropic and OpenAI; the call path is dormant.

**Custody note.** Holding user API keys makes Jolt responsible for financial credentials — a breach is a billing breach on the user's account. This is accepted deliberately for the future feature set, but it raises the security bar: Managed Identity everywhere, no key material in images or env, Key Vault access logged, and the validation-at-capture step so Jolt never stores a key it hasn't confirmed the user actually owns.

**MVP boundary.** For MVP, having a key stored unlocks nothing yet — it is captured and held. The later features it enables (on-demand doc processing without waiting for a sync, question-answering, other intelligence tasks) are the second, key-backed mode of operation. When they arrive, they are the only paths that put inference on Jolt's own compute, and they remain strictly opt-in and billed to the user's key.

---

## 6. Data-access layer

### Partition strategy

Cosmos performance and cost are dominated by the partition key. The rule for Jolt:

| Container | Partition key | Rationale |
|---|---|---|
| `users` | `/id` | Point reads by user |
| `tracks` | `/user_id` (curated: `/id`) | User's tracks co-located; curated read by id |
| `sources` | `/user_id` | All of a user's sources in one logical partition |
| `extractions` | `/source_id` | Extraction history for a source read together |
| `sessions` | `/user_id` | |
| `concepts` | `/track_id` | Concepts of a track together; works for curated too |
| `concept_states` | `/user_id` | A user's whole memory state co-located — the hot path |
| `questions` | `/concept_id` | Questions for a concept read together |
| `reviews` | `/user_id` | The review backlog and history, per user |
| `answer_feedback` | `/review_id` | |

`concept_states` partitioned by `user_id` is the important one: the nudge job and the review flow both read "everything due for this user," and that must be a single-partition query, not a cross-partition fan-out.

### Query discipline

Per the spec's RU note, filtered/sorted queries cost more than point reads. Concrete rules:

- `jolt_get_coverage`: read the track's concepts (single partition on `track_id`) plus the user's `concept_states`, join in memory. No cross-partition query.
- `jolt_get_pending_gradings` / `jolt_get_unprocessed_sources`: single-partition query on `user_id`, filtered by status, ordered by timestamp, `LIMIT n`. Index the status + timestamp fields so it stays cheap.
- Due-review query: single-partition on `user_id`, `WHERE due_at <= now`. Composite index on `(user_id, due_at)`.

### Repositories

One repository class per aggregate. Each takes `user_id` as a required constructor or method argument and refuses to build a query without it. Repositories return domain models (Pydantic), never raw Cosmos dicts, so `domain/` never sees Azure types.

### Leases

`get_unprocessed_sources` and `get_pending_gradings` both mark rows `in_flight` with `lease_expires_at = now + T`. A row is claimable if status is `unprocessed`/`pending` **or** (`in_flight` and lease expired). Acquire is a conditional write (Cosmos optimistic concurrency via `etag`), so two concurrent syncs cannot both claim the same row. `jolt_submit_gradings` / `jolt_store_extraction` clear the lease on success. The FSRS job's cleanup pass resets any long-expired leases, closing the interrupted-sync gap.

---

## 7. Request lifecycles

### 7.1 Direct upload (Flutter app, no agent)

```
App → POST /v1/uploads/request  { filename, content_type, sha256 }
        auth dep resolves user_id
        create sources row: processing_status = 'unprocessed'
        generate Blob SAS PUT url (short TTL, scoped to one blob path)
      ← { source_id, upload_url }
App → PUT upload_url  (bytes straight to Blob, bypasses Jolt compute)
App → POST /v1/uploads/{source_id}/confirm
        verify blob exists, size, sha256 matches
        mark source ready-for-processing
      ← 200
```

Jolt compute never touches the bytes. The file sits `unprocessed` until a sync claims it.

### 7.2 Scheduled sync (agent, unattended)

```
Scheduled task (Claude/Codex) connects to /mcp with the user's token
Agent calls jolt_sync
  → server returns the plan: N unprocessed sources, M pending gradings
Agent calls jolt_get_unprocessed_sources { limit }
  → rows marked in_flight (lease)
For each source:
  jolt_get_source_content { source_id }  → image block / signed GET url
  (agent runs extraction on its own subscription)
  jolt_store_extraction { source_id, markdown, confidence, model_id }
  jolt_log_session { concepts[...] }        → concepts created/attached
  jolt_create_questions { concept_id, questions[...] }
  → source marked processed, lease cleared
Agent calls jolt_get_pending_gradings { limit }  → rows in_flight
  (agent grades free-text against expected_answer + rubric)
  jolt_submit_gradings { gradings[...] }   → answer_feedback written,
                                              suggested_fsrs_grade stored,
                                              reviews marked graded
Backend: grade changes enqueue affected concept_states for FSRS recompute
```

Everything the agent does runs on the user's subscription. Jolt writes rows.

### 7.3 Two-stage review (Flutter app)

```
App → GET /v1/reviews/due            → due questions (single-partition query)
App → POST /v1/reviews/{q}/stage1  { free_text }
        create review row, free_text_locked_at = now, status pending-grading
      ← { options, ... }   (stage-2 payload released only now)
App → POST /v1/reviews/{q}/stage2  { selected_index }
        is_correct = (selected == correct_index)
        provisional_fsrs_grade = synthesise(is_correct, stage1_len, latency)
        apply provisional grade to concept_state, set due_at
      ← { correct, expected_answer }
```

The stage-2 options are not sent until stage-1 is submitted — the API enforces the lock server-side, so a modified client cannot peek. Provisional grade lands immediately; the semantic final grade arrives later via sync (7.2) and the FSRS job reschedules.

### 7.4 FSRS recompute (job)

```
Trigger: cron + a "dirty" flag on concept_states touched by new grades
For each dirty concept_state:
  load full review history for that (user, concept)
  fold FSRS over the history, preferring final_fsrs_grade over provisional
  write new stability/difficulty/retrievability/due_at
  clear dirty flag
Also: reset expired in_flight leases (source + review) to claimable
```

Full-history fold rather than incremental step is deliberate — it is what lets a retroactive final grade correct an earlier provisional one (spec §7).

### 7.5 Viewing due tasks in-app (replaces the nudge for MVP)

```
App → GET /v1/reviews/due
        auth dep resolves user_id
        single-partition query on concept_states WHERE due_at <= now
        join pre-generated questions
      ← due count + question stems
```

Pull, not push. The same due query that a nudge job would have run executes on app open instead. No timer, no email, no timezone handling for MVP.

### 7.6 API-key capture (MVP)

```
App → POST /v1/settings/api-key  { provider, key }   (TLS)
        provider adapter runs a cheap validation probe (no billed call)
        on success:
          store raw key in Key Vault  (secret: user_id + provider)
          write Cosmos reference { provider, key_ref, last4, added_at }
        on failure: reject, store nothing
      ← { provider, last4, status: 'validated' }

App → DELETE /v1/settings/api-key/{provider}
        purge Key Vault secret, then Cosmos reference
      ← 204
```

Storage only for MVP — no feature reads the key yet. `credentials/inference.py` is the dormant seam for later.

### 7.7 Re-extraction with supersede (agent)

```
Agent calls jolt_get_source_content { source_id }   (re-reads original)
Agent calls jolt_store_extraction { ..., supersede: true }
Backend reconciliation (domain/reconciliation.py):
  diff new extraction vs active extraction → per-concept classification
  unchanged   → no-op
  refined     → update concept text, keep questions + reviews + state
  changed     → supersede concept, create successor, carry state,
                retire questions, stamp reviews invalidated_by_extraction_id
  removed     → supersede concept, drop from due queue, keep for history
  new         → create concept, (agent) generates questions, schedule
  move active_extraction_id pointer
Mark affected concept_states dirty → FSRS job excludes invalidated reviews
```

This is the most dangerous write path in the system (spec risk #8), so it runs inside a single logical unit with careful ordering: history is stamped before pointers move, so a crash mid-reconciliation leaves the old active extraction canonical and replayable.

---

## 8. Concurrency and consistency

Cosmos serverless gives single-document atomicity and optimistic concurrency via `etag`, not multi-document transactions (outside a single partition's transactional batch). Jolt's design leans on this deliberately:

- **Lease claims** are single-document conditional writes — no lock table.
- **Reconciliation** touches many documents; it is made safe by ordering (stamp-before-move) and idempotency (re-running a partially-applied supersede converges), not by a distributed transaction.
- **Grade submission** is idempotent on `review_id`, so an agent retry after a dropped connection does not double-write.
- Where several documents in one partition must change together (e.g. a concept and its state), Cosmos' **transactional batch** within the `user_id` partition is used.

The guiding assumption: every write path can be safely retried. Agents drop connections, scheduled tasks get killed, syncs interrupt. Idempotency is not optional.

---

## 9. Configuration, secrets, observability

- **Config** via `pydantic-settings` from environment; Container Apps injects secrets as env refs to Key Vault.
- **Secrets** — token signing pepper, Cosmos keys, Blob keys, ACS keys — all in Key Vault, never in image or repo. Prefer Managed Identity for Cosmos/Blob/Key Vault so there are no stored keys at all where Azure supports it.
- **Observability** — Application Insights via OpenTelemetry. The metrics that matter: sync frequency per user (habit health, spec risk #6), extraction confidence distribution (silent-misextraction canary, risk #2), lease-expiry rate (interrupted-sync signal), RU consumption per tool (cost control).
- **Budget alert** on the subscription at first provision (spec §11).

---

## 10. Deployment and environments

- **Container image**: single image, two entrypoints (`api`, `job-fsrs`) selected by command. One build, two Container Apps resources. The `job-nudge` entrypoint is deferred with the nudge feature.
- **IaC**: Bicep or Terraform for the Container Apps environment, Cosmos account, storage account, Key Vault. (ACS/email deferred with the nudge.)
- **Environments**: `dev` and `prod` as separate resource groups. Cosmos' 25 GB + 1000 RU/s free tier covers dev entirely.
- **CI/CD**: build image → run `domain/` unit tests (no Azure needed) → deploy to dev → smoke-test MCP handshake + a round-trip sync → promote to prod.
- **Migrations**: Cosmos is schema-flexible, so migrations are code-level (model version fields) not DDL. New optional fields are additive; document shape changes are versioned per-document and read-time upgraded.

---

## 11. Open questions (LLD-specific)

- **Key-backed feature execution locus (deferred).** When the later intelligence features are built, each one is either a server-side call (Jolt retrieves the key, calls the model, inference on Jolt's compute) or a client-handoff (the app calls the model directly). Storing the key server-side keeps both open; decide per feature when built, not now. On-demand doc processing leans server-side; interactive question-answering could go either way.
- **MCP transport auth mechanics.** Exactly how the per-user token rides the MCP connection (header vs. init param) depends on the client. Verify against Claude Desktop's and Codex's current MCP config format before finalising `auth/context.py`.
- **Session boundary.** `jolt_log_session` assumes one session per sync. If an overnight sync processes three days of backlog uploads, is that one session or three (by upload date)? Affects the understanding-over-time timeline granularity.
- **Reconciliation atomicity ceiling.** A supersede that reclassifies 50 concepts exceeds a single transactional-batch partition limit. Need a documented max, or a resumable multi-batch reconciliation with a checkpoint.
- **Curated content storage.** Do curated tracks live in the same Cosmos containers with `user_id: null`, or a separate read-only container? Same-container is simpler; separate isolates the global-read path more strictly.
- **Cold-start on FSRS job.** Scale-to-zero on a cron job means a cold container each run. Fine for a nightly recompute; verify the FSRS fold over a large user's history finishes inside the job timeout.