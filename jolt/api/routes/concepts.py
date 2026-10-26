"""Concept routes (BE-06): unretire, question versions.

BE-07 adds the concept read model (`GET /v1/concepts/{id}`) and corrections to
this router. A concept is the caller's when they hold a card (FSRS state) for
it; any other id is a 404.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from jolt.api.deps import RuntimeDep, UserDep
from jolt.api.errors import CONFLICT, NOT_FOUND, UNAUTHORIZED
from jolt.domain.models import Question, QuestionStatus
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
