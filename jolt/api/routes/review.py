"""Review routes — two-stage flow, inline FSRS (LLD §7.3, §7.5).

Note on paths: stage 1 is addressed by question id; stage 2 by the review id
returned from stage 1 (a question can be reviewed many times, so the mutable
stage-2 submission is keyed to the specific review). The server enforces the
stage lock — options are only returned by stage 1.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from jolt.api.deps import RuntimeDep, UserDep
from jolt.services.reviews import ReviewError, ReviewService

router = APIRouter(prefix="/v1/reviews", tags=["reviews"])


class DueItemResponse(BaseModel):
    concept_id: str
    question_id: str
    stem: str


class DueResponse(BaseModel):
    count: int
    items: list[DueItemResponse]


class StageOneRequest(BaseModel):
    concept_id: str
    free_text: str


class StageOneResponse(BaseModel):
    review_id: str
    options: list[str]


class StageTwoRequest(BaseModel):
    selected_index: int
    latency_ms: int | None = None


class StageTwoResponse(BaseModel):
    correct: bool
    expected_answer: str
    correct_index: int
    due_at: datetime | None


@router.get("/due", response_model=DueResponse)
async def get_due(user: UserDep, rt: RuntimeDep, limit: int = 100) -> DueResponse:
    items = await ReviewService(rt).due(user.user_id, limit=limit)
    return DueResponse(
        count=len(items),
        items=[DueItemResponse(**item.__dict__) for item in items],
    )


@router.post("/{question_id}/stage1", response_model=StageOneResponse)
async def submit_stage_one(
    question_id: str, body: StageOneRequest, user: UserDep, rt: RuntimeDep
) -> StageOneResponse:
    try:
        result = await ReviewService(rt).submit_stage_one(
            user.user_id, body.concept_id, question_id, body.free_text
        )
    except ReviewError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return StageOneResponse(review_id=result.review_id, options=result.options)


@router.post("/{review_id}/stage2", response_model=StageTwoResponse)
async def submit_stage_two(
    review_id: str, body: StageTwoRequest, user: UserDep, rt: RuntimeDep
) -> StageTwoResponse:
    try:
        result = await ReviewService(rt).submit_stage_two(
            user.user_id, review_id, body.selected_index, body.latency_ms
        )
    except ReviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return StageTwoResponse(
        correct=result.correct,
        expected_answer=result.expected_answer,
        correct_index=result.correct_index,
        due_at=result.due_at,
    )
