"""Progress routes — understanding-over-time and concept detail (LLD §4 progress)."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from jolt.api.deps import RuntimeDep, UserDep

router = APIRouter(prefix="/v1/progress", tags=["progress"])


class ConceptStatePoint(BaseModel):
    reviewed_at: datetime | None
    grade: int | None
    is_correct: bool | None


class ConceptDetailResponse(BaseModel):
    concept_id: str
    stability: float
    difficulty: float
    retrievability: float
    due_at: datetime | None
    reps: int
    lapses: int
    timeline: list[ConceptStatePoint]


@router.get("/concepts/{concept_id}", response_model=ConceptDetailResponse)
async def concept_detail(
    concept_id: str, user: UserDep, rt: RuntimeDep
) -> ConceptDetailResponse:
    state = await rt.repos.concept_states.for_concept(user.user_id, concept_id)
    if state is None:
        raise HTTPException(status_code=404, detail="no state for this concept")
    history = await rt.repos.reviews.history_for_concept(user.user_id, concept_id)
    timeline = [
        ConceptStatePoint(
            reviewed_at=r.free_text_locked_at or r.created_at,
            grade=r.final_fsrs_grade or r.provisional_fsrs_grade,
            is_correct=r.is_correct,
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
