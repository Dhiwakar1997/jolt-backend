"""Progress routes — understanding-over-time and concept detail (LLD §4 progress)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from jolt.api.deps import RuntimeDep, UserDep
from jolt.api.errors import NOT_FOUND, UNAUTHORIZED
from jolt.services.reviews import grading_of

router = APIRouter(prefix="/v1/progress", tags=["progress"], responses=UNAUTHORIZED)


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
    retrievability: float
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
        retrievability=state.retrievability,
        due_at=state.due_at,
        reps=state.reps,
        lapses=state.lapses,
        timeline=timeline,
    )
