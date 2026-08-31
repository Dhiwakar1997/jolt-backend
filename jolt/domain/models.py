"""Domain models — Pydantic representations of the spec §5 data model.

These double as the persisted document shape (Cosmos, schema-flexible) and the
in-process domain objects. `domain/` imports no FastAPI and no Azure SDK, so
every model here is unit-testable in isolation.

Document-versioning: each document carries `schema_version` so shape changes can
be read-time upgraded (LLD §10, migrations are code-level not DDL).
"""

from __future__ import annotations

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


class FSRSGrade(int, Enum):
    """FSRS ratings — the four-button scale."""

    AGAIN = 1
    HARD = 2
    GOOD = 3
    EASY = 4


# --------------------------------------------------------------------------- #
# Aggregates
# --------------------------------------------------------------------------- #
class User(DocBase):
    # Only the Argon2 hash of the opaque token is stored; plaintext shown once.
    token_hash: str
    display_name: Optional[str] = None
    retention_days: Optional[int] = None  # data-retention preference


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


class Source(DocBase):
    user_id: str
    filename: str
    content_type: str
    sha256: str
    blob_path: str
    size_bytes: Optional[int] = None
    processing_status: ProcessingStatus = ProcessingStatus.UNPROCESSED
    # Lease bookkeeping (LLD §6 leases).
    lease_expires_at: Optional[datetime] = None
    active_extraction_id: Optional[str] = None
    confirmed_at: Optional[datetime] = None


class Extraction(DocBase):
    source_id: str
    markdown: str
    confidence: float = 0.0
    model_id: Optional[str] = None
    status: ExtractionStatus = ExtractionStatus.ACTIVE
    superseded_by: Optional[str] = None


class Session(DocBase):
    user_id: str
    source_ids: list[str] = Field(default_factory=list)
    concept_ids: list[str] = Field(default_factory=list)


class Concept(DocBase):
    track_id: str
    source_id: Optional[str] = None
    extraction_id: Optional[str] = None
    text: str
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


class Question(DocBase):
    concept_id: str
    stem: str
    options: list[str] = Field(default_factory=list)
    correct_index: int = 0
    expected_answer: str = ""
    status: QuestionStatus = QuestionStatus.ACTIVE


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
