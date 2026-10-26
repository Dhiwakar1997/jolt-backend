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

from pydantic import BaseModel, Field

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
class UserSettings(BaseModel):
    """Per-user preferences stored on the user document.

    BE-06 adds the two review behaviours; BE-08 adds the rest of the Settings
    screen (theme, desired retention, daily cap, data retention) here.
    """

    # Retire a concept from the review queue once it is mastered (FSRS stability
    # at or above MASTERY_STABILITY_DAYS after a Good or Easy answer).
    retire_mastered: bool = True
    # Decision D8: question rewriting after grading is opt-in per user. When on,
    # a grading's `rewritten_question` becomes a new question version.
    allow_question_rewrite: bool = False


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
    retention_days: Optional[int] = None  # data-retention preference
    settings: UserSettings = Field(default_factory=UserSettings)


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

    def keys(self) -> set[str]:
        return {item.concept_key for item in self.syllabus}


class Track(DocBase):
    # Curated tracks have user_id = None and are world-readable.
    user_id: Optional[str] = None
    name: str
    origin: TrackOrigin = TrackOrigin.USER
    agenda: Agenda = Field(default_factory=Agenda)


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


class Session(DocBase):
    user_id: str
    source_ids: list[str] = Field(default_factory=list)
    concept_ids: list[str] = Field(default_factory=list)


class Concept(DocBase):
    track_id: str
    source_id: Optional[str] = None
    extraction_id: Optional[str] = None
    text: str
    # Short label from jolt_log_session (BE-06: queue items and summaries).
    title: Optional[str] = None
    status: ConceptStatus = ConceptStatus.ACTIVE
    successor_id: Optional[str] = None  # set when superseded
    syllabus_ref: Optional[str] = None  # coverage linkage


class ConceptState(DocBase):
    """A user's FSRS memory state for one concept — the hot path (LLD §6)."""

    user_id: str
    concept_id: str
    track_id: Optional[str] = None
    # FSRS card state.
    stability: float = 0.0
    difficulty: float = 0.0
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
