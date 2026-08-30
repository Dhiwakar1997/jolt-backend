"""Source routes — source list and extraction history (LLD §4 sources)."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from jolt.api.deps import RuntimeDep, UserDep

router = APIRouter(prefix="/v1/sources", tags=["sources"])


class SourceResponse(BaseModel):
    id: str
    filename: str
    content_type: str
    processing_status: str
    active_extraction_id: str | None
    created_at: datetime


class ExtractionResponse(BaseModel):
    id: str
    source_id: str
    confidence: float
    model_id: str | None
    status: str
    created_at: datetime


@router.get("", response_model=list[SourceResponse])
async def list_sources(user: UserDep, rt: RuntimeDep) -> list[SourceResponse]:
    sources = await rt.repos.sources.list_for_user(user.user_id)
    return [
        SourceResponse(
            id=s.id,
            filename=s.filename,
            content_type=s.content_type,
            processing_status=s.processing_status.value,
            active_extraction_id=s.active_extraction_id,
            created_at=s.created_at,
        )
        for s in sources
    ]


@router.get("/{source_id}/extractions", response_model=list[ExtractionResponse])
async def extraction_history(
    source_id: str, user: UserDep, rt: RuntimeDep
) -> list[ExtractionResponse]:
    # Confirm ownership before reading the (source-partitioned) extraction history.
    source = await rt.repos.sources.get_source(source_id, user.user_id)
    if source is None:
        raise HTTPException(status_code=404, detail="source not found")
    history = await rt.repos.extractions.history_for_source(source_id)
    return [
        ExtractionResponse(
            id=e.id,
            source_id=e.source_id,
            confidence=e.confidence,
            model_id=e.model_id,
            status=e.status.value,
            created_at=e.created_at,
        )
        for e in history
    ]
