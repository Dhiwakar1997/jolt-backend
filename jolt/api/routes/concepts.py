"""Concept routes: the Concept screen in one call, corrections (BE-07); unretire,
question versions (BE-06).

A concept is the caller's when they hold a card (FSRS state) for it; any other
id is a 404 (`jolt.services.ownership`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from jolt.api.deps import RuntimeDep, UserDep
from jolt.api.errors import (
    CONFLICT,
    KEY_BACKED_ERRORS,
    NOT_FOUND,
    UNAUTHORIZED,
    KeyBackedErrorResponse,
)
from jolt.api.key_backed import ModelUsageResponse, Provider, usage_response
from jolt.api.routes.tracks import PatternLibraryResponse
from jolt.domain.models import Question, QuestionStatus
from jolt.services.corrections import CorrectionConflict, CorrectionService
from jolt.services.feedback import FeedbackService
from jolt.services.ownership import NotOwned
from jolt.services.read_models import ReadModelService
from jolt.services.reviews import ReviewConflict, ReviewError, ReviewService

router = APIRouter(prefix="/v1/concepts", tags=["concepts"], responses=UNAUTHORIZED)


class UnretireResponse(BaseModel):
    concept_id: str
    retired: Literal[False] = False
    due_at: Optional[datetime]


class QuestionVersionResponse(BaseModel):
    question_id: str
    version: int
    stem: str
    previous_version_id: Optional[str]
    rewrite_reason: Optional[str]
    created_at: datetime
    active: bool


class QuestionVersionsResponse(BaseModel):
    concept_id: str
    current_question: Optional[QuestionVersionResponse] = Field(
        description="The version the next session serves"
    )
    question_versions: list[QuestionVersionResponse] = Field(description="Oldest first (D8)")


def version_view(q: Question) -> QuestionVersionResponse:
    return QuestionVersionResponse(
        question_id=q.id,
        version=q.version,
        stem=q.stem,
        previous_version_id=q.previous_version_id,
        rewrite_reason=q.rewrite_reason,
        created_at=q.created_at,
        active=q.status == QuestionStatus.ACTIVE,
    )


@router.post(
    "/{concept_id}/unretire", response_model=UnretireResponse, responses={**NOT_FOUND, **CONFLICT}
)
async def unretire_concept(concept_id: str, user: UserDep, rt: RuntimeDep) -> UnretireResponse:
    try:
        state = await ReviewService(rt).unretire(user.user_id, concept_id)
    except ReviewConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ReviewError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return UnretireResponse(concept_id=concept_id, due_at=state.due_at)


@router.get("/{concept_id}/questions", response_model=QuestionVersionsResponse, responses=NOT_FOUND)
async def list_question_versions(
    concept_id: str, user: UserDep, rt: RuntimeDep
) -> QuestionVersionsResponse:
    try:
        versions, current = await ReviewService(rt).question_versions(user.user_id, concept_id)
    except ReviewError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return QuestionVersionsResponse(
        concept_id=concept_id,
        current_question=version_view(current) if current else None,
        question_versions=[version_view(q) for q in versions],
    )


# --------------------------------------------------------------------------- #
# Concept detail (BE-07 §3)
# --------------------------------------------------------------------------- #
class ConceptFoundIn(BaseModel):
    source_id: str
    source_title: str
    page: Optional[int] = Field(description="1-based page of the source, when the model said")
    uploaded_at: datetime


class TimelineEntryResponse(BaseModel):
    review_id: str
    reviewed_at: datetime
    free_text: str = Field(description="The learner's own words (stage 1)")
    is_correct: Optional[bool]
    grade: Optional[int] = Field(description="FSRS 1–4: provisional, then the model's")
    grading: Literal["provisional", "graded"]
    feedback: Optional[str] = Field(description="The model's feedback, once graded")
    question_id: str
    question_version: int
    interval_days: Optional[int] = Field(description="Days since the previous answer")


class FsrsResponse(BaseModel):
    stability: float
    difficulty: float
    retrievability: float = Field(description="Predicted recall now, 0–1 (0 before any review)")
    due_at: Optional[datetime] = Field(description="Next review; null when retired or superseded")
    reps: int
    lapses: int


class CorrectionNoteResponse(BaseModel):
    note: str
    created_at: datetime


class ConceptDetailResponse(BaseModel):
    id: str
    track_id: str
    track_name: str
    title: str
    summary: str = Field(description="What the model wrote about the concept")
    concept_key: Optional[str]
    state: Literal["new", "learning", "due", "mastered"]
    retired: bool
    off_plan: bool
    status: Literal["active", "superseded"] = Field(
        description="superseded: a re-read replaced (successor_id) or removed it"
    )
    successor_id: Optional[str]
    found_in: Optional[ConceptFoundIn]
    timeline: list[TimelineEntryResponse] = Field(description="Every answer, oldest first")
    current_question: Optional[QuestionVersionResponse] = Field(
        description="The version the next session serves"
    )
    question_versions: list[QuestionVersionResponse] = Field(description="Oldest first (D8)")
    adaptive_note: Optional[str] = Field(
        description="Why the current version exists (its rewrite reason); null for v1"
    )
    fsrs: FsrsResponse
    pending_corrections: list[CorrectionNoteResponse] = Field(
        description="Notes waiting for the next sync"
    )
    library: Optional[PatternLibraryResponse] = Field(
        default=None, description="A DSA pattern's library entry (BE-09); null otherwise"
    )
    created_at: datetime


@router.get("/{concept_id}", response_model=ConceptDetailResponse, responses=NOT_FOUND)
async def get_concept(concept_id: str, user: UserDep, rt: RuntimeDep) -> ConceptDetailResponse:
    """The Concept screen: summary, where found, answer timeline, question versions
    with the adaptive note, FSRS state."""
    try:
        d = await ReadModelService(rt).concept_detail(user.user_id, concept_id, user.user.settings)
    except NotOwned as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    c = d.concept
    return ConceptDetailResponse(
        id=c.id,
        track_id=c.track_id,
        track_name=d.track_name,
        title=d.title,
        summary=c.text,
        concept_key=c.syllabus_ref,
        state=d.state,
        retired=d.retired,
        off_plan=d.off_plan,
        status=c.status.value,
        successor_id=c.successor_id,
        found_in=ConceptFoundIn(**d.found_in.__dict__) if d.found_in else None,
        timeline=[TimelineEntryResponse(**e.__dict__) for e in d.timeline],
        current_question=version_view(d.current_question) if d.current_question else None,
        question_versions=[version_view(q) for q in d.question_versions],
        adaptive_note=d.adaptive_note,
        fsrs=FsrsResponse(**d.fsrs.__dict__),
        pending_corrections=[
            CorrectionNoteResponse(note=n.note, created_at=n.created_at)
            for n in d.pending_corrections
        ],
        library=PatternLibraryResponse(**d.library.model_dump()) if d.library else None,
        created_at=c.created_at,
    )


class CorrectionRequest(BaseModel):
    note: str = Field(min_length=1, max_length=2000, description="What is wrong, in your words")


class CorrectionResponse(BaseModel):
    concept_id: str
    note: str
    status: Literal["queued"] = "queued"
    created_at: datetime


@router.post(
    "/{concept_id}/correction",
    response_model=CorrectionResponse,
    responses={**NOT_FOUND, **CONFLICT},
)
async def request_correction(
    concept_id: str, body: CorrectionRequest, user: UserDep, rt: RuntimeDep
) -> CorrectionResponse:
    """ "Something's wrong with this concept": queue a note; the next sync reads it
    (jolt_get_pending_corrections) and fixes the concept with jolt_correct_concept."""
    if not body.note.strip():
        raise HTTPException(status_code=422, detail="note must not be blank")
    try:
        entry = await CorrectionService(rt).request(user.user_id, concept_id, body.note)
    except NotOwned as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except CorrectionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return CorrectionResponse(concept_id=concept_id, note=entry.note, created_at=entry.created_at)


# --------------------------------------------------------------------------- #
# Understanding feedback on the user's own key (BE-11)
# --------------------------------------------------------------------------- #
class FeedbackRequest(BaseModel):
    review_id: Optional[str] = Field(
        default=None,
        max_length=64,
        description="Explain this one answer (the result screen) instead of the whole timeline",
    )
    provider: Optional[Provider] = Field(
        default=None, description="Whose key pays; default: the most recently added key"
    )


class FeedbackResponse(BaseModel):
    concept_id: str
    review_id: Optional[str] = Field(description="The answer explained; null: the whole timeline")
    feedback: str = Field(description="How your understanding has moved (2–4 sentences)")
    next_step: str = Field(description="One concrete thing to try next")
    provider: Provider
    model_id: Optional[str] = Field(description="The model that wrote it")
    answers: int = Field(description="Answers in the timeline it read")
    generated_at: datetime
    cached: bool = Field(
        description="Served from the cache (no model call): the timeline has not changed since"
    )
    usage: ModelUsageResponse


@router.post(
    "/{concept_id}/feedback",
    response_model=FeedbackResponse,
    responses={
        **NOT_FOUND,
        **KEY_BACKED_ERRORS,
        409: {
            "model": KeyBackedErrorResponse,
            "description": "`no_answers`: the concept has no answer yet",
        },
    },
)
async def concept_feedback(
    concept_id: str, user: UserDep, rt: RuntimeDep, body: FeedbackRequest | None = None
) -> FeedbackResponse:
    """Feedback on your understanding, from your answer timeline, on your own API key.

    Synchronous (runs while the app waits). Cached on your card until your next
    answer or grade, so asking again costs nothing. Without a stored key:
    403 `key_required` and no outbound call."""
    body = body or FeedbackRequest()
    try:
        r = await FeedbackService(rt).feedback(
            user.user_id, concept_id, review_id=body.review_id, provider=body.provider
        )
    except NotOwned as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return FeedbackResponse(
        concept_id=r.concept_id,
        review_id=r.review_id,
        feedback=r.feedback,
        next_step=r.next_step,
        provider=r.provider,
        model_id=r.model_id,
        answers=r.answers,
        generated_at=r.generated_at,
        cached=r.cached,
        usage=usage_response(r.usage, r.input_tokens, r.output_tokens),
    )
