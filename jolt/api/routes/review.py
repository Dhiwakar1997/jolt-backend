"""Review routes — sessions, two-stage flow, inline FSRS (LLD §7.3, §7.5, BE-06).

Note on paths: stage 1 is addressed by question id; stage 2 by the review id
returned from stage 1 (a question can be reviewed many times, so the mutable
stage-2 submission is keyed to the specific review). The server enforces the
stage lock — options are only returned by stage 1; queue items and the due list
carry the stem only.

Both stages are idempotent on an optional `client_request_id`: a retry with the
same id returns the first response instead of failing or writing twice.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from jolt.api.deps import RuntimeDep, UserDep
from jolt.api.errors import BAD_REQUEST, CONFLICT, NOT_FOUND, UNAUTHORIZED
from jolt.services.curated import is_pattern_match, public_concept_id
from jolt.services.reviews import (
    ReviewConflict,
    ReviewError,
    ReviewNotFound,
    ReviewService,
    grading_of,
)

router = APIRouter(prefix="/v1/reviews", tags=["reviews"], responses=UNAUTHORIZED)
sessions_router = APIRouter(prefix="/v1/review-sessions", tags=["reviews"], responses=UNAUTHORIZED)

Grading = Literal["provisional", "graded"]
ClientRequestId = Field(
    default=None,
    min_length=1,
    max_length=128,
    description="Idempotency key: a retry with the same id returns the first response",
)


def _raise(exc: ReviewError, default_status: int) -> None:
    if isinstance(exc, ReviewNotFound):
        raise HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ReviewConflict):
        raise HTTPException(status_code=409, detail=str(exc))
    raise HTTPException(status_code=default_status, detail=str(exc))


# --------------------------------------------------------------------------- #
# Due list (LLD §7.5)
# --------------------------------------------------------------------------- #
class DueItemResponse(BaseModel):
    concept_id: str
    question_id: str
    stem: str


class DueResponse(BaseModel):
    count: int
    items: list[DueItemResponse]


@router.get("/due", response_model=DueResponse)
async def get_due(user: UserDep, rt: RuntimeDep, limit: int = 100) -> DueResponse:
    items = await ReviewService(rt).due(user.user_id, limit=limit)
    return DueResponse(
        count=len(items),
        items=[DueItemResponse(**item.__dict__) for item in items],
    )


# --------------------------------------------------------------------------- #
# Stage 1 and stage 2
# --------------------------------------------------------------------------- #
class StageOneRequest(BaseModel):
    concept_id: str
    free_text: str
    client_request_id: Optional[str] = ClientRequestId
    session_id: Optional[str] = Field(
        default=None, description="The review session this answer belongs to"
    )


class StageOneResponse(BaseModel):
    review_id: str
    options: list[str]
    locked_at: datetime


class StageTwoRequest(BaseModel):
    selected_index: int
    latency_ms: int | None = None
    client_request_id: Optional[str] = ClientRequestId


class StageTwoResponse(BaseModel):
    correct: bool
    expected_answer: str
    correct_index: int
    due_at: datetime | None
    grading: Grading
    provisional_grade: int = Field(ge=1, le=4, description="FSRS grade set from the choice")
    interval_days: int = Field(description="Days until the next review, at least 1")
    match: Optional[Literal["correct", "partial", "incorrect"]] = Field(
        default=None,
        description="pattern_match only: correct (the pattern), partial (an acceptable one), "
        "incorrect; null for free_text",
    )
    question_title: Optional[str] = Field(
        default=None,
        description="pattern_match only: the problem's title, revealed now; null for free_text",
    )
    retired: bool = Field(description="The concept is mastered and out of the queue")
    feedback: Optional[str] = Field(
        default=None,
        description="The model's feedback, once graded (pattern_match: the coaching line, "
        "graded at once)",
    )
    concept_id: Optional[str] = Field(
        default=None,
        description="The answered concept (for pattern_match, the pattern the queue item withheld)",
    )
    leetcode_url: Optional[str] = Field(
        default=None, description="pattern_match only: the problem on leetcode.com"
    )


@router.post(
    "/{question_id}/stage1",
    response_model=StageOneResponse,
    responses={**NOT_FOUND, **CONFLICT},
)
async def submit_stage_one(
    question_id: str, body: StageOneRequest, user: UserDep, rt: RuntimeDep
) -> StageOneResponse:
    try:
        result = await ReviewService(rt).submit_stage_one(
            user.user_id,
            body.concept_id,
            question_id,
            body.free_text,
            client_request_id=body.client_request_id,
            session_id=body.session_id,
        )
    except ReviewError as exc:
        _raise(exc, 404)
    return StageOneResponse(
        review_id=result.review_id, options=result.options, locked_at=result.locked_at
    )


@router.post("/{review_id}/stage2", response_model=StageTwoResponse, responses=BAD_REQUEST)
async def submit_stage_two(
    review_id: str, body: StageTwoRequest, user: UserDep, rt: RuntimeDep
) -> StageTwoResponse:
    try:
        result = await ReviewService(rt).submit_stage_two(
            user.user_id,
            review_id,
            body.selected_index,
            body.latency_ms,
            client_request_id=body.client_request_id,
        )
    except ReviewError as exc:
        _raise(exc, 400)
    return StageTwoResponse(**result.__dict__)


class ReviewResponse(BaseModel):
    review_id: str
    concept_id: str
    question_id: str
    question_version: int
    session_id: Optional[str]
    free_text: Optional[str]
    locked_at: Optional[datetime]
    selected_index: Optional[int]
    correct: Optional[bool]
    provisional_grade: Optional[int]
    final_grade: Optional[int]
    grading: Grading
    feedback: Optional[str] = Field(description="The model's feedback summary, once graded")
    due_after: Optional[datetime]
    rewritten_question_id: Optional[str] = Field(
        description="The question version a rewrite created from this answer"
    )


@router.get("/{review_id}", response_model=ReviewResponse, responses=NOT_FOUND)
async def get_review(review_id: str, user: UserDep, rt: RuntimeDep) -> ReviewResponse:
    try:
        review, question = await ReviewService(rt).get_review(user.user_id, review_id)
    except ReviewError as exc:
        _raise(exc, 404)
    graded = grading_of(review) == "graded"
    concept_id = review.concept_id
    if is_pattern_match(question) and review.selected_index is None:
        concept_id = public_concept_id(question, concept_id)  # the answer, until stage 2
    return ReviewResponse(
        review_id=review.id,
        concept_id=concept_id,
        question_id=review.question_id,
        question_version=question.version if question else review.question_version,
        session_id=review.session_id,
        free_text=review.free_text,
        locked_at=review.free_text_locked_at,
        selected_index=review.selected_index,
        correct=review.is_correct,
        provisional_grade=review.provisional_fsrs_grade,
        final_grade=review.final_fsrs_grade,
        grading=grading_of(review),
        feedback=review.feedback if graded else None,
        due_after=review.due_after,
        rewritten_question_id=review.rewritten_question_id,
    )


# --------------------------------------------------------------------------- #
# Review sessions (BE-06 §1)
# --------------------------------------------------------------------------- #
class StartSessionRequest(BaseModel):
    track_id: Optional[str] = Field(default=None, description="Only this track's due questions")
    limit: int = Field(default=100, ge=1, le=500, description="Most questions in the queue")
    concept_id: Optional[str] = Field(
        default=None, description="Practise one concept now, due or not (ignores the rest)"
    )
    kind: Optional[Literal["notes", "dsa_patterns"]] = Field(
        default=None,
        description="dsa_patterns: a DSA session drawn by the v1 rules from the caller's "
        "enrolled DSA tracks (BE-09); notes: due questions without the DSA tracks; "
        "omitted: every due question",
    )
    time_budget_minutes: Optional[int] = Field(
        default=None,
        ge=1,
        le=240,
        description="DSA sessions: the one continuous clock, in minutes (default 30); "
        "problems are drawn until their time weights fill it",
    )
    difficulty_mix: Optional["DifficultyMix"] = Field(
        default=None, description="DSA sessions: difficulty weights (default 20 / 55 / 25)"
    )


class DifficultyMix(BaseModel):
    """Relative weights of the difficulties in a DSA draw (need not sum to 100)."""

    easy: float = Field(default=20, ge=0)
    medium: float = Field(default=55, ge=0)
    hard: float = Field(default=25, ge=0)


StartSessionRequest.model_rebuild()


class PatternMatchExample(BaseModel):
    input: str
    output: str
    note: Optional[str] = None


class PatternMatch(BaseModel):
    """A `pattern_match` problem as the queue shows it (BE-09). Never the title or
    the LeetCode link: both name the problem, and stage 2 reveals them."""

    description: str
    io_contract: str = Field(description="`Input: …` and `Output: …` lines")
    io_input: str = Field(description="What comes in, in words")
    io_output: str = Field(description="What goes out, in words")
    examples: list[PatternMatchExample] = Field(description="`note`: the example's explanation")
    difficulty: Literal["Easy", "Medium", "Hard"]
    time_weight: int = Field(description="Minutes it takes of the session clock (2 / 4 / 7)")


class QueueItemResponse(BaseModel):
    concept_id: str = Field(
        description="pattern_match: the track id (the pattern is the answer, withheld until "
        "stage 2); send it back as stage 1's concept_id"
    )
    concept_title: str = Field(description='pattern_match: "Pattern match"')
    question_id: str
    question_type: Literal["free_text", "pattern_match"]
    stem: str
    track_id: str
    track_name: str
    version: int
    interval_days: Optional[int] = Field(description="Days since the last review; null if first")
    review_number: int
    rewritten: bool = Field(description="This version was rewritten after the last answer")
    pattern_match: Optional[PatternMatch] = None


class ReviewSessionResponse(BaseModel):
    id: str
    started_at: datetime
    track_id: Optional[str]
    items: list[QueueItemResponse]
    done_today: int = Field(description="Answers given since midnight UTC")
    time_budget_minutes: Optional[int] = Field(
        default=None, description="DSA sessions: the session clock (null otherwise)"
    )
    time_weight_total: Optional[int] = Field(
        default=None, description="DSA sessions: minutes the drawn problems weigh (≤ the budget)"
    )


class ConceptRefResponse(BaseModel):
    concept_id: str
    title: str


class MovedResponse(BaseModel):
    concept_id: str
    title: str
    old_due_at: Optional[datetime]
    new_due_at: Optional[datetime] = Field(description="null when the concept was retired")


class UpcomingDayResponse(BaseModel):
    date: date
    questions: int


class SessionSummaryResponse(BaseModel):
    session_id: str
    started_at: datetime
    ended_at: datetime
    reviewed: int
    correct: int
    provisional: int
    graded: int
    retired: list[ConceptRefResponse]
    moved: list[MovedResponse]
    upcoming: list[UpcomingDayResponse]


@sessions_router.post("", response_model=ReviewSessionResponse, responses=NOT_FOUND)
async def start_review_session(
    body: StartSessionRequest, user: UserDep, rt: RuntimeDep
) -> ReviewSessionResponse:
    try:
        view = await ReviewService(rt).start_session(
            user.user_id,
            track_id=body.track_id,
            limit=body.limit,
            concept_id=body.concept_id,
            kind=body.kind,
            time_budget_minutes=body.time_budget_minutes,
            difficulty_mix=(
                {
                    "Easy": body.difficulty_mix.easy,
                    "Medium": body.difficulty_mix.medium,
                    "Hard": body.difficulty_mix.hard,
                }
                if body.difficulty_mix
                else None
            ),
        )
    except ReviewError as exc:
        _raise(exc, 404)
    return ReviewSessionResponse(
        id=view.id,
        started_at=view.started_at,
        track_id=view.track_id,
        items=[QueueItemResponse(**item.__dict__) for item in view.items],
        done_today=view.done_today,
        time_budget_minutes=view.time_budget_minutes,
        time_weight_total=view.time_weight_total,
    )


@sessions_router.post(
    "/{session_id}/end", response_model=SessionSummaryResponse, responses=NOT_FOUND
)
async def end_review_session(
    session_id: str, user: UserDep, rt: RuntimeDep
) -> SessionSummaryResponse:
    try:
        s = await ReviewService(rt).end_session(user.user_id, session_id)
    except ReviewError as exc:
        _raise(exc, 404)
    return SessionSummaryResponse(
        session_id=s.session_id,
        started_at=s.started_at,
        ended_at=s.ended_at,
        reviewed=s.reviewed,
        correct=s.correct,
        provisional=s.provisional,
        graded=s.graded,
        retired=[ConceptRefResponse(**r.__dict__) for r in s.retired],
        moved=[MovedResponse(**m.__dict__) for m in s.moved],
        upcoming=[UpcomingDayResponse(**u.__dict__) for u in s.upcoming],
    )
