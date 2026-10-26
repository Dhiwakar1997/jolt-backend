"""Domain models — Pydantic representations of the spec §5 data model.

These double as the persisted document shape (Cosmos, schema-flexible) and the
in-process domain objects. `domain/` imports no FastAPI and no Azure SDK, so
every model here is unit-testable in isolation.

Document-versioning: each document carries `schema_version` so shape changes can
be read-time upgraded (LLD §10, migrations are code-level not DDL).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator

SCHEMA_VERSION = 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


class DocBase(BaseModel):
    """Common fields for every persisted document."""

    id: str = Field(default_factory=_new_id)
    schema_version: int = SCHEMA_VERSION
    created_at: datetime = Field(default_factory=_now)
    # Cosmos optimistic-concurrency tag; populated on read, sent on conditional write.
    etag: Optional[str] = Field(default=None, alias="_etag")

    model_config = {"populate_by_name": True}


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class ProcessingStatus(str, Enum):
    UNPROCESSED = "unprocessed"
    IN_FLIGHT = "in_flight"
    PROCESSED = "processed"
    # The source could not be read: its sync lease lapsed `SOURCE_MAX_ATTEMPTS`
    # times without an extraction (BE-05). Read again resets it to unprocessed.
    FAILED = "failed"


class ExtractionStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class ConceptStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class QuestionStatus(str, Enum):
    ACTIVE = "active"
    RETIRED = "retired"


class ReviewStatus(str, Enum):
    PENDING_GRADING = "pending_grading"
    GRADED = "graded"
    INVALIDATED = "invalidated"
    # BE-09: a `pattern_match` review between its two stages. Its grade is
    # deterministic (set at stage 2), so it never waits for the sync's grader.
    AWAITING_CHOICE = "awaiting_choice"


class TrackOrigin(str, Enum):
    USER = "user"
    JOLT = "jolt"  # curated, world-readable (LLD §5 curated-track exception)


class AgendaStatus(str, Enum):
    """Lifecycle of a track's agenda (the shaped syllabus)."""

    NONE = "none"  # no agenda yet
    DRAFT = "draft"  # syllabus written but not yet marked refined
    REFINED = "refined"  # agent-shaped and current
    LOCKED = "locked"  # frozen: refinement and auto-add are rejected/skipped


class AgendaSource(str, Enum):
    """Who last shaped the agenda."""

    AGENT = "agent"  # written by a sync-pass agent
    USER = "user"  # authored/edited by the learner
    EMERGENT = "emergent"  # grew from logged concepts (auto-add)


class TokenKind(str, Enum):
    """Which surface an opaque token opens (BE-03)."""

    APP = "app"  # jolt_app_…: the app, on /v1
    MCP = "mcp"  # jolt_live_…: a connected Claude / Codex client, on /mcp


class FSRSGrade(int, Enum):
    """FSRS ratings — the four-button scale."""

    AGAIN = 1
    HARD = 2
    GOOD = 3
    EASY = 4


# --------------------------------------------------------------------------- #
# Aggregates
# --------------------------------------------------------------------------- #
class Theme(str, Enum):
    LIGHT = "light"
    DARK = "dark"
    SYSTEM = "system"


# FSRS desired retention a learner may pick on Settings · Reviews (BE-08).
DESIRED_RETENTION_MIN = 0.80
DESIRED_RETENTION_MAX = 0.97


class UserSettings(BaseModel):
    """Per-user preferences stored on the user document.

    BE-06 added the two review behaviours; BE-08 the rest of the Settings
    screen. Every field has a default, so documents written before a field
    existed read as "the default".
    """

    # Retire a concept from the review queue once it is mastered (FSRS stability
    # at or above MASTERY_STABILITY_DAYS after a Good or Easy answer).
    retire_mastered: bool = True
    # Decision D8: question rewriting after grading is opt-in per user. When on,
    # a grading's `rewritten_question` becomes a new question version.
    allow_question_rewrite: bool = False
    # Appearance (BE-08). The app applies it; the server only stores it.
    theme: Theme = Theme.SYSTEM
    # FSRS desired retention (0.80-0.97). None: the server default
    # (FSRS_DESIRED_RETENTION). Used by every FSRS fold of this user's cards, so
    # a change shapes the intervals of the next answers (BE-08).
    desired_retention: Optional[float] = None
    # Most answers a day; review sessions are cut to what is left of it.
    # None: no cap.
    daily_review_cap: Optional[int] = None
    # How long to keep uploads, in days. The daily maintenance sweep (BE-10,
    # jolt/services/maintenance.py) deletes older sources with their blobs and
    # reads. None: keep forever. Was `User.retention_days` / PUT /v1/settings/retention.
    data_retention_days: Optional[int] = None
    # IANA zone of the learner's day: "today", streaks, the heatmap, the upload
    # date of a session (decision D6) and the sync schedule (GAPS #8).
    time_zone: str = "UTC"


class Enrolment(BaseModel):
    """The learner added a curated track (BE-09). Their cards on it are created
    lazily, by the first review."""

    track_id: str
    enrolled_at: datetime = Field(default_factory=_now)


class User(DocBase):
    """A Jolt account (BE-03).

    The id is derived from the sign-in identity (see `user_id_for`), so the same
    Google account always maps to the same user without a cross-partition query.
    Tokens live in their own container (`AuthToken`).
    """

    email: Optional[str] = None
    display_name: Optional[str] = None
    picture_url: Optional[str] = None
    auth_provider: Optional[str] = None  # "google" | "dev"
    auth_subject: Optional[str] = None  # Google `sub`, or the dev email
    last_sign_in_at: Optional[datetime] = None
    # Set by the first successful nightly sync (BE-04); drives onboarding.
    first_sync_at: Optional[datetime] = None
    # Legacy home of the data-retention preference (before BE-08); read-time
    # migrated into `settings.data_retention_days`, written back as None.
    retention_days: Optional[int] = None
    settings: UserSettings = Field(default_factory=UserSettings)
    # BE-09: curated tracks the learner enrolled in (kept on the user document:
    # /v1/me, Tracks and the review queue read it with the user, no extra query).
    enrolments: list[Enrolment] = Field(default_factory=list)
    # BE-11: today's key-backed model usage (the per-user spend guard).
    model_usage: Optional["ModelUsage"] = None

    def enrolled_track_ids(self) -> set[str]:
        return {e.track_id for e in self.enrolments}

    @model_validator(mode="after")
    def _migrate_retention_days(self) -> "User":
        if self.retention_days is not None:
            if self.settings.data_retention_days is None:
                self.settings.data_retention_days = self.retention_days
            self.retention_days = None
        return self


class ModelUsage(BaseModel):
    """Key-backed model calls one user made on one UTC day (BE-11 spend guard).

    `requests` counts every call that reached a provider (a failed one too);
    tokens are what the provider reported. A new day starts from zero."""

    day: str  # YYYY-MM-DD, UTC
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def user_id_for(provider: str, subject: str) -> str:
    """Stable, opaque user id for a sign-in identity (32 hex chars)."""
    return hashlib.sha256(f"{provider}:{subject}".encode()).hexdigest()[:32]


class AuthToken(DocBase):
    """One opaque token: an app token (a signed-in device) or an MCP token (BE-03).

    Partitioned by user so listing and revoking are single-partition. Resolution
    (the pre-auth path) finds the document by `lookup`, an HMAC of the token, and
    verifies `token_hash` (Argon2). Revoking deletes the document.
    """

    user_id: str
    kind: TokenKind
    lookup: str
    token_hash: str
    label: Optional[str] = None
    last4: str
    last_used_at: Optional[datetime] = None


class SyllabusItem(BaseModel):
    """One entry in an agenda's syllabus — a concept the track means to cover.

    `concept_key` is the stable join key: a concept's `syllabus_ref` points at it,
    coverage matches on it, and auto-add tests membership by it. `parent` (another
    item's `concept_key`) makes the syllabus a shallow tree; None means a leaf/root.
    """

    concept_key: str
    label: str
    parent: Optional[str] = None
    # Per-item provenance (BE-07): who added this item — the learner (an outline
    # or a syllabus at creation), an agent's refine, or auto-add from uploads
    # (emergent). None on items written before BE-07: read as the agenda's source.
    source: Optional[AgendaSource] = None


class Agenda(BaseModel):
    """A track's shaped syllabus plus its refinement lifecycle (track agenda model).

    The backend only stores agendas; it never generates or infers them. Agents shape
    the syllabus (jolt_set_agenda) and per-concept growth (auto-add on log_session)
    extends it. `locked` freezes both write paths.
    """

    status: AgendaStatus = AgendaStatus.NONE
    source: AgendaSource = AgendaSource.AGENT
    syllabus: list[SyllabusItem] = Field(default_factory=list)
    last_refined_at: Optional[datetime] = None
    refined_from_source_id: Optional[str] = None
    # BE-07: the learner's rough outline from track creation, kept verbatim for
    # the next sync to tidy into a refined syllabus (the draft items are a
    # mechanical first cut of it).
    outline: Optional[str] = None

    def keys(self) -> set[str]:
        return {item.concept_key for item in self.syllabus}

    def item_source(self, item: SyllabusItem) -> AgendaSource:
        return item.source or self.source


class TrackKind(str, Enum):
    NOTES = "notes"
    DSA_PATTERNS = "dsa_patterns"  # curated DSA tracks (BE-09)


class Track(DocBase):
    # Curated tracks have user_id = None and are world-readable.
    user_id: Optional[str] = None
    name: str
    origin: TrackOrigin = TrackOrigin.USER
    agenda: Agenda = Field(default_factory=Agenda)
    # BE-07: what the track holds, and when the learner archived it (None:
    # active). An archived track's concepts leave the review queue.
    kind: TrackKind = TrackKind.NOTES
    archived_at: Optional[datetime] = None
    # BE-09: the catalog blurb of a curated track (None on learners' tracks).
    description: Optional[str] = None


class ProblemIndexEntry(BaseModel):
    """One playable problem of a curated DSA track, in the track content's index:
    everything the session draw needs, so a draw is one point read (BE-09)."""

    question_id: str
    concept_id: str
    leetcode_id: int
    difficulty: str  # Easy | Medium | Hard
    time_weight: int  # minutes: Easy 2, Medium 4, Hard 7
    near_duplicate_of: Optional[int] = None


class TrackContent(DocBase):
    """Track-level content of a curated track (BE-09): the decision tree and the
    problem index. Stored in the `tracks` container, curated bucket, with id
    `<track_id>.content`; it has no `origin`, so the catalog query skips it."""

    doc_type: str = "track_content"
    track_id: str
    kind: TrackKind = TrackKind.DSA_PATTERNS
    # The pattern names in answer-grid order (every pattern_match question's options).
    patterns: list[str] = Field(default_factory=list)
    decision_tree: dict = Field(default_factory=dict)
    problems: list[ProblemIndexEntry] = Field(default_factory=list)
    imported_at: Optional[datetime] = None
    # Hash of everything above, so an unchanged re-import writes nothing.
    content_hash: Optional[str] = None

    @staticmethod
    def id_for(track_id: str) -> str:
        return f"{track_id}.content"


class SourcePage(BaseModel):
    """One uploaded file of a source (BE-05, decision A7: one source, N pages)."""

    index: int  # 0-based, upload order
    filename: str
    content_type: str
    sha256: str  # lowercase hex, as declared at request time
    blob_path: str
    declared_size: Optional[int] = None  # bytes, from the upload request
    size_bytes: Optional[int] = None  # bytes Blob reported at confirm


class Source(DocBase):
    """An upload: one or more files read together as one note (BE-05).

    `filename`, `content_type`, `sha256` and `blob_path` describe the first page,
    which keeps documents written before BE-05 (single file, no `pages`) valid;
    `page_list()` reads both shapes.
    """

    user_id: str
    filename: str
    content_type: str
    sha256: str
    blob_path: str
    size_bytes: Optional[int] = None  # total over all pages, set at confirm
    processing_status: ProcessingStatus = ProcessingStatus.UNPROCESSED
    # Lease bookkeeping (LLD §6 leases).
    lease_expires_at: Optional[datetime] = None
    active_extraction_id: Optional[str] = None
    confirmed_at: Optional[datetime] = None
    # BE-05: the track the note was added to (None: uploaded without one, e.g.
    # by an agent), a display title, and the pages.
    track_id: Optional[str] = None
    title: Optional[str] = None
    pages: list[SourcePage] = Field(default_factory=list)
    # Claims by a sync, and when the latest one was taken (failure detection and
    # read-again bookkeeping).
    claim_count: int = 0
    # Reads stored for this source (the Sources table's "Reads" column).
    read_count: int = 0
    claimed_at: Optional[datetime] = None
    failed_reason: Optional[str] = None
    # Read again (BE-05 §4): set by the app, cleared by the extraction that
    # answers it.
    reread_requested_at: Optional[datetime] = None
    reread_preferred_model: Optional[str] = None

    @property
    def uploaded_at(self) -> datetime:
        """When the upload finished (confirm), else when it was requested. The
        app's "uploaded at" and the session day (decision D6) both use it."""
        return self.confirmed_at or self.created_at

    def page_list(self) -> list[SourcePage]:
        if self.pages:
            return sorted(self.pages, key=lambda p: p.index)
        return [
            SourcePage(
                index=0,
                filename=self.filename,
                content_type=self.content_type,
                sha256=self.sha256,
                blob_path=self.blob_path,
                size_bytes=self.size_bytes,
            )
        ]

    @property
    def kind(self) -> str:
        """`photos` (all images), `pdf` (all PDFs) or `file` (a mix)."""
        types = [p.content_type for p in self.page_list()]
        if all(t.startswith("image/") for t in types):
            return "photos"
        if all(t == "application/pdf" for t in types):
            return "pdf"
        return "file"


class ExtractionConcept(BaseModel):
    """A concept a read logged ("Concepts it touched")."""

    concept_id: str
    title: str
    is_new: bool = True  # first found by this read


class Extraction(DocBase):
    """One read of a source. `concepts` and `questions_written` are kept on the
    read itself (filled by jolt_log_session / jolt_create_questions) so Past reads
    is one single-partition query (BE-05)."""

    source_id: str
    markdown: str
    confidence: float = 0.0
    model_id: Optional[str] = None
    status: ExtractionStatus = ExtractionStatus.ACTIVE
    superseded_by: Optional[str] = None
    # The MCP token's label, e.g. "Claude Desktop" (BE-05).
    client_label: Optional[str] = None
    concepts: list[ExtractionConcept] = Field(default_factory=list)
    questions_written: int = 0
    # BE-07: the supersede reconciliation this read drives, with its checkpoint
    # (None when the read was stored without `concepts`).
    reconciliation: Optional["ReconciliationCheckpoint"] = None
    # BE-11: how the read was made: None = a nightly sync (MCP), "read_now" =
    # POST /v1/sources/{id}/read-now on the user's own key; `provider` names it.
    via: Optional[str] = None
    provider: Optional[str] = None


class ProposedConceptDoc(BaseModel):
    """A concept a re-read proposes (the `concepts` of jolt_store_extraction)."""

    text: str
    track_id: str
    title: Optional[str] = None
    concept_key: Optional[str] = None
    parent: Optional[str] = None
    page: Optional[int] = None


class ReconcileStep(BaseModel):
    """One planned reconciliation change. `concept_id` is fixed when the plan is
    made (existing id, or a deterministic id for a new or successor concept), so
    re-applying a step after a crash converges on the same documents."""

    change: str  # unchanged | refined | changed | removed | new
    index: Optional[int] = None  # position in the proposed list; None for removed
    existing_concept_id: Optional[str] = None
    existing_track_id: Optional[str] = None
    concept_id: Optional[str] = None  # the concept that carries on (None: removed)
    proposed: Optional[ProposedConceptDoc] = None
    similarity: float = 0.0


class ReconciliationCheckpoint(BaseModel):
    """Resumable supersede reconciliation (BE-07 §6, LLD §11 atomicity ceiling).

    The whole plan is classified up front and stored on the new read; steps are
    applied in order, at most `RECONCILE_MAX_CONCEPTS` per call, and `cursor`
    (steps applied) is saved after each one. `jolt_continue_reconciliation`
    resumes from the cursor.
    """

    status: str = "in_progress"  # in_progress | complete
    previous_extraction_id: Optional[str] = None
    steps: list[ReconcileStep] = Field(default_factory=list)
    cursor: int = 0
    updated_at: datetime = Field(default_factory=_now)


Extraction.model_rebuild()


class Session(DocBase):
    user_id: str
    source_ids: list[str] = Field(default_factory=list)
    concept_ids: list[str] = Field(default_factory=list)
    # Decision D6 (BE-04): one session per upload date, so the timeline stays per
    # day when a sync works through a multi-day backlog. YYYY-MM-DD (UTC) of the
    # session's sources; None when the session has no source.
    upload_date: Optional[str] = None
    # The sync run that logged it, when it was logged inside one (BE-04).
    sync_run_id: Optional[str] = None


class Concept(DocBase):
    track_id: str
    source_id: Optional[str] = None
    extraction_id: Optional[str] = None
    text: str
    # Short label from the agent (jolt_log_session `title`), when it gave one;
    # also shown on review queue items and session summaries (BE-06).
    title: Optional[str] = None
    status: ConceptStatus = ConceptStatus.ACTIVE
    successor_id: Optional[str] = None  # set when superseded
    syllabus_ref: Optional[str] = None  # coverage linkage
    # BE-07: the 1-based page of the source it was found on ("where found").
    page: Optional[int] = None
    superseded_at: Optional[datetime] = None
    # BE-09: Jolt-authored content on a curated track (shared by every learner,
    # read-only), and for a DSA pattern its library entry.
    curated: bool = False
    library: Optional["PatternLibrary"] = None


class PatternLibrary(BaseModel):
    """A DSA pattern's library entry (v1 `src/tracks/dsa/content/library.ts`)."""

    category: str = ""
    time: str = ""
    space: str = ""
    when_to_reach: str = ""
    tells: list[str] = Field(default_factory=list)
    pseudocode: list[str] = Field(default_factory=list)
    explanation: list[str] = Field(default_factory=list)
    # The step animation for the pattern (v1 `animations.ts` reel, by slug).
    animation_id: Optional[str] = None


Concept.model_rebuild()


class CarriedCard(BaseModel):
    """The FSRS card a successor concept inherits from the concept it replaced
    (supersede, change class `changed`). The successor's fold starts from it
    instead of a blank card, so its schedule carries on."""

    stability: float = 0.0
    difficulty: float = 0.0
    due_at: Optional[datetime] = None
    last_review_at: Optional[datetime] = None
    reps: int = 0
    lapses: int = 0
    fsrs_state: int = 0
    step: int = 0


class CorrectionNote(BaseModel):
    """ "Something's wrong with this concept": a note for the next sync (BE-07 §4)."""

    note: str
    created_at: datetime = Field(default_factory=_now)


class ConceptState(DocBase):
    """A user's FSRS memory state for one concept — the hot path (LLD §6)."""

    user_id: str
    concept_id: str
    track_id: Optional[str] = None
    # FSRS card state.
    stability: float = 0.0
    difficulty: float = 0.0
    # Not maintained: predicted recall changes by the minute, so readers compute
    # it from stability + last_review_at (`jolt.domain.progress.retrievability`).
    retrievability: float = 0.0
    due_at: Optional[datetime] = None
    last_review_at: Optional[datetime] = None
    reps: int = 0
    lapses: int = 0
    fsrs_state: int = 0  # py-fsrs State enum value (0=new,1=learning,2=review,3=relearning)
    step: int = 0
    # Recompute bookkeeping.
    dirty: bool = False
    # Mastery (BE-06 §5). `mastered_at` is the time of the latest review while
    # the card is at or above the mastery threshold, None below it; it is
    # recomputed from the history on every fold. `unretired_at` is when the user
    # brought the concept back (POST /v1/concepts/{id}/unretire); the concept is
    # retired again only by a later review that is still mastered. Whether a
    # mastered concept leaves the queue also depends on the user's
    # `retire_mastered` setting: see `jolt.domain.mastery.is_retired`.
    mastered_at: Optional[datetime] = None
    unretired_at: Optional[datetime] = None
    # BE-07 read-model denormalisation, so Track detail needs no query per
    # concept. `active_question_ids` are the concept's active questions in
    # rotation order (the queue serves `[reps % n]`); None on cards written
    # before BE-07 (backfilled on first read). `question_version` is the newest
    # active version. `last_answer` is the free text of the latest folded review.
    active_question_ids: Optional[list[str]] = None
    question_version: Optional[int] = None
    last_answer: Optional[str] = None
    # Supersede (BE-07): the concept was replaced or removed by a re-read; the
    # card stays for history but is never due again.
    superseded_at: Optional[datetime] = None
    carried: Optional[CarriedCard] = None
    carried_from: Optional[str] = None
    # Pending corrections for the next sync; `correction_requested_at` is set
    # while any are pending (the sync's single-partition query keys on it).
    corrections: list[CorrectionNote] = Field(default_factory=list)
    correction_requested_at: Optional[datetime] = None
    # BE-11: the last understanding feedback (concept page) and the last
    # explanation of one answer (result screen), each kept until the answer
    # timeline changes (`fingerprint`: a new answer, or a grade landing).
    feedback: Optional["FeedbackCache"] = None
    explanation: Optional["FeedbackCache"] = None


class FeedbackCache(BaseModel):
    """A key-backed feedback answer cached on the learner's card (BE-11)."""

    fingerprint: str
    review_id: Optional[str] = None  # the answer explained (explanations only)
    feedback: str
    next_step: str
    provider: str
    model_id: Optional[str] = None
    answers: int = 0
    generated_at: datetime = Field(default_factory=_now)


class QuestionType(str, Enum):
    FREE_TEXT = "free_text"
    PATTERN_MATCH = "pattern_match"  # a curated DSA problem (BE-09)


class ProblemExample(BaseModel):
    input: str
    output: str
    explanation: Optional[str] = None


class PatternMatchContent(BaseModel):
    """What a `pattern_match` question (a v1 DSA problem) carries besides the
    common fields. `options` are all the patterns, `correct_index` the problem's
    `algorithm`, `Question.acceptable_indexes` its `acceptableAlgorithms`."""

    track_id: str
    leetcode_id: int
    leetcode_url: str
    description: str
    io_input: str = ""
    io_output: str = ""
    examples: list[ProblemExample] = Field(default_factory=list)
    difficulty: str  # Easy | Medium | Hard
    time_weight: int
    near_duplicate_of: Optional[int] = None
    batch: Optional[str] = None  # the v1 batch file it came from


class Question(DocBase):
    """A review question. Every rewrite is a new version (decision D8).

    Versions of one question form a chain through `previous_version_id` (and
    `superseded_by` forwards). All versions live in the concept's partition and
    are kept; only the newest is `active` and served by the queue.
    """

    concept_id: str
    stem: str
    options: list[str] = Field(default_factory=list)
    correct_index: int = 0
    expected_answer: str = ""
    status: QuestionStatus = QuestionStatus.ACTIVE
    version: int = 1
    previous_version_id: Optional[str] = None
    rewrite_reason: Optional[str] = None
    # The grading (review) whose rewrite produced this version.
    rewritten_from_review_id: Optional[str] = None
    superseded_by: Optional[str] = None  # the next version's id
    retired_at: Optional[datetime] = None
    # BE-09: `pattern_match` questions (curated DSA problems). `title` is the
    # problem's real name: it is the answer, so it is served only after stage 2.
    type: QuestionType = QuestionType.FREE_TEXT
    title: Optional[str] = None
    acceptable_indexes: list[int] = Field(default_factory=list)  # score partial
    pattern_match: Optional[PatternMatchContent] = None


class Review(DocBase):
    user_id: str
    question_id: str
    concept_id: str
    # Stage 1 (free text).
    free_text: Optional[str] = None
    free_text_locked_at: Optional[datetime] = None
    # Stage 2 (multiple choice).
    selected_index: Optional[int] = None
    is_correct: Optional[bool] = None
    latency_ms: Optional[int] = None
    # Grades.
    provisional_fsrs_grade: Optional[int] = None
    final_fsrs_grade: Optional[int] = None
    status: ReviewStatus = ReviewStatus.PENDING_GRADING
    invalidated_by_extraction_id: Optional[str] = None
    # BE-06: the review session it belongs to, the question version answered,
    # idempotency keys for both stages, and what the answer moved.
    session_id: Optional[str] = None
    question_version: int = 1
    stage1_request_id: Optional[str] = None
    stage2_request_id: Optional[str] = None
    stage2_at: Optional[datetime] = None
    due_before: Optional[datetime] = None
    due_after: Optional[datetime] = None
    retired_now: bool = False  # this answer retired the concept (mastered)
    # The model's feedback summary, set with the final grade.
    feedback: Optional[str] = None
    # The question version a grading's rewrite created from this review.
    rewritten_question_id: Optional[str] = None
    # BE-09 `pattern_match`: correct | partial | incorrect, set at stage 2.
    match: Optional[str] = None


class ReviewSession(DocBase):
    """A review session: the ordered queue served by POST /v1/review-sessions.

    `started_at` is `created_at`. The summary is computed from the session's
    reviews when it ends, so ending twice returns the same summary.
    """

    user_id: str
    track_id: Optional[str] = None
    concept_id: Optional[str] = None
    kind: Optional[str] = None
    limit: int = 0
    # BE-09 DSA sessions: the one continuous clock and what the queue fills of it.
    time_budget_minutes: Optional[int] = None
    time_weight_total: Optional[int] = None
    question_ids: list[str] = Field(default_factory=list)
    concept_ids: list[str] = Field(default_factory=list)
    ended_at: Optional[datetime] = None


class AnswerFeedback(DocBase):
    review_id: str
    user_id: str
    suggested_fsrs_grade: int
    rationale: Optional[str] = None
    rubric_version: Optional[str] = None


class ApiKeyRef(DocBase):
    """Cosmos-side reference to a per-user provider key (LLD §5.1).

    The plaintext lives only in Key Vault. This holds no key material.
    """

    user_id: str
    provider: str
    key_ref: str  # Key Vault secret name
    last4: str
    added_at: datetime = Field(default_factory=_now)
    last_validated_at: Optional[datetime] = None


# --------------------------------------------------------------------------- #
# Nightly sync runs (BE-04)
# --------------------------------------------------------------------------- #
class SyncRunStatus(str, Enum):
    RUNNING = "running"  # opened by jolt_sync, still taking tool calls
    FINISHED = "finished"  # closed by a jolt_sync that found nothing left
    INCOMPLETE = "incomplete"  # the client went quiet before a final jolt_sync


class SyncChangeKind(str, Enum):
    CONCEPT_ADDED = "concept_added"  # jolt_log_session created it
    CONCEPT_UPDATED = "concept_updated"  # jolt_correct_concept rewrote its text
    QUESTION_REWRITTEN = "question_rewritten"  # reserved for question versions (BE-06)


class SyncChange(BaseModel):
    """One thing a sync run changed, for Today's "New since last sync"."""

    kind: SyncChangeKind
    concept_id: str
    concept_title: str
    track_id: str
    source_id: Optional[str] = None
    source_uploaded_at: Optional[datetime] = None
    added_to_syllabus: bool = False


class SyncRun(DocBase):
    """One nightly sync: opened by `jolt_sync`, fed by the tool calls made with the
    same MCP token, closed by the `jolt_sync` that finds nothing left (or by going
    idle). Partitioned by user (BE-04)."""

    user_id: str
    token_id: str
    client_label: str
    status: SyncRunStatus = SyncRunStatus.RUNNING
    started_at: datetime = Field(default_factory=_now)
    finished_at: Optional[datetime] = None
    last_activity_at: datetime = Field(default_factory=_now)
    # Sources this run claimed; a claimed, unfinished one keeps the run open.
    claimed_source_ids: list[str] = Field(default_factory=list)
    processed_source_ids: list[str] = Field(default_factory=list)
    questions_written: int = 0
    gradings_submitted: int = 0
    errors: list[str] = Field(default_factory=list)
    changes: list[SyncChange] = Field(default_factory=list)
    plan_calls: int = 0

    @property
    def sources_processed(self) -> int:
        return len(self.processed_source_ids)

    @property
    def did_work(self) -> bool:
        return bool(self.processed_source_ids or self.questions_written or self.gradings_submitted)


# BE-11 forward references.
User.model_rebuild()
ConceptState.model_rebuild()
