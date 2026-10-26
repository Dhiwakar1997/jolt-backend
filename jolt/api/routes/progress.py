"""Progress routes — the Progress screen and concept detail (LLD §4 progress, BE-08)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from jolt.api.deps import RuntimeDep, UserDep
from jolt.api.errors import NOT_FOUND, UNAUTHORIZED
from jolt.domain.progress import retrievability
from jolt.services.progress import ProgressService
from jolt.services.reviews import grading_of

router = APIRouter(prefix="/v1/progress", tags=["progress"], responses=UNAUTHORIZED)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ConceptStatePoint(BaseModel):
    reviewed_at: datetime | None
    grade: int | None
    is_correct: bool | None
    # BE-06: which answer, of which question version, and its grading state.
    review_id: str | None = None
    question_id: str | None = None
    question_version: int = 1
    grading: Literal["provisional", "graded"] = "provisional"
    feedback: str | None = None


class ConceptDetailResponse(BaseModel):
    concept_id: str
    stability: float
    difficulty: float
    retrievability: float = Field(
        description="Predicted recall now (0-1) from stability and the time since the last "
        "review; about 1 right after a review, 0 before the first"
    )
    due_at: datetime | None
    reps: int
    lapses: int
    timeline: list[ConceptStatePoint]


@router.get("/concepts/{concept_id}", response_model=ConceptDetailResponse, responses=NOT_FOUND)
async def concept_detail(concept_id: str, user: UserDep, rt: RuntimeDep) -> ConceptDetailResponse:
    state = await rt.repos.concept_states.for_concept(user.user_id, concept_id)
    if state is None:
        raise HTTPException(status_code=404, detail="no state for this concept")
    history = await rt.repos.reviews.history_for_concept(user.user_id, concept_id)
    timeline = [
        ConceptStatePoint(
            reviewed_at=r.free_text_locked_at or r.created_at,
            grade=r.final_fsrs_grade or r.provisional_fsrs_grade,
            is_correct=r.is_correct,
            review_id=r.id,
            question_id=r.question_id,
            question_version=r.question_version,
            grading=grading_of(r),
            feedback=r.feedback if grading_of(r) == "graded" else None,
        )
        for r in history
    ]
    return ConceptDetailResponse(
        concept_id=concept_id,
        stability=state.stability,
        difficulty=state.difficulty,
        retrievability=retrievability(state.stability, state.last_review_at, _now()),
        due_at=state.due_at,
        reps=state.reps,
        lapses=state.lapses,
        timeline=timeline,
    )


# --------------------------------------------------------------------------- #
# GET /v1/progress (BE-08)
# --------------------------------------------------------------------------- #
class WeeklyRetention(BaseModel):
    week_start: date = Field(description="Monday of the week, in the user's time_zone")
    retention: float = Field(description="Mean predicted recall of active concepts at week end")


class DailyReviews(BaseModel):
    date: date
    reviews: int = Field(description="Answers (stage 2) given that day")


class TrackProgress(BaseModel):
    track_id: str
    name: str
    kind: Literal["notes", "dsa_patterns"]
    retention: float = Field(description="Mean predicted recall of the track's active concepts")
    due: int = Field(description="Concepts due now (retired ones left out)")
    mastered: int
    learning: int = Field(description="Studied and not mastered")


class ProgressResponse(BaseModel):
    weeks: int
    retention: float = Field(
        description="Mean predicted recall (0-1) now of active concepts: studied at least "
        "once and not retired. 0 when there are none"
    )
    retention_weekly: list[WeeklyRetention] = Field(
        description="Oldest first, one point per week of the window that had active concepts; "
        "the current week's point is now"
    )
    reviews_total: int = Field(description="Answers in the window (sum of `daily`)")
    reviews_this_week: int = Field(description="Answers since Monday (user's time_zone)")
    answers_in_own_words: int = Field(
        description="Stage 1 answers with any text in the window (answered or not)"
    )
    streak_days: int = Field(
        description="Consecutive days with an answer, up to today (or yesterday while "
        "nothing is answered yet today); not limited to the window"
    )
    concepts_learning: int = Field(description="Studied and not mastered")
    concepts_mastered: int
    sources_read: int = Field(description="Confirmed uploads with an active read")
    daily: list[DailyReviews] = Field(
        description="One entry per day of the window (weeks x 7 days up to today), oldest first"
    )
    tracks: list[TrackProgress] = Field(
        description="The user's tracks (newest first), then curated tracks they study"
    )


@router.get("", response_model=ProgressResponse)
async def get_progress(
    user: UserDep,
    rt: RuntimeDep,
    weeks: int = Query(default=26, ge=1, le=104, description="Window length in weeks"),
) -> ProgressResponse:
    p = await ProgressService(rt).progress(user.user_id, weeks)
    return ProgressResponse(
        weeks=p.weeks,
        retention=p.retention,
        retention_weekly=[WeeklyRetention(**w.__dict__) for w in p.retention_weekly],
        reviews_total=p.reviews_total,
        reviews_this_week=p.reviews_this_week,
        answers_in_own_words=p.answers_in_own_words,
        streak_days=p.streak_days,
        concepts_learning=p.concepts_learning,
        concepts_mastered=p.concepts_mastered,
        sources_read=p.sources_read,
        daily=[DailyReviews(**d.__dict__) for d in p.daily],
        tracks=[TrackProgress(**t.__dict__) for t in p.tracks],
    )
