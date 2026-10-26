"""Source routes: list, detail with past reads, read again, extraction history,
storage figure (LLD §4 sources, BE-05)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from jolt.api.deps import RuntimeDep, UserDep
from jolt.api.errors import NOT_FOUND, UNAUTHORIZED
from jolt.domain.models import ProcessingStatus
from jolt.services.sources import SourceNotFound, SourceService

router = APIRouter(prefix="/v1/sources", tags=["sources"], responses=UNAUTHORIZED)
storage_router = APIRouter(prefix="/v1/storage", tags=["sources"], responses=UNAUTHORIZED)

StatusLiteral = Literal["unprocessed", "in_flight", "processed", "failed"]


class _FromAttrs(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RereadState(_FromAttrs):
    requested_at: datetime
    preferred_model: Optional[str]


class SourceResponse(_FromAttrs):
    id: str
    filename: str = Field(description="The first page's filename")
    content_type: str = Field(description="The first page's content type")
    processing_status: StatusLiteral
    active_extraction_id: str | None
    created_at: datetime
    title: str
    track_id: Optional[str]
    track_name: Optional[str]
    kind: Literal["photos", "pdf", "file"]
    page_count: int
    uploaded_at: datetime = Field(description="When the upload was confirmed")
    concepts_found: int = Field(description="Concepts the active read logged")
    questions_written: int = Field(description="Questions written for those concepts")
    low_confidence: bool = Field(
        description="The active read's confidence is under LOW_CONFIDENCE_THRESHOLD"
    )
    reread_requested: Optional[RereadState] = Field(
        description="Read again requested and not yet done"
    )
    read_count: int = Field(description="Reads (extractions) stored for this source")


class SourcePageResponse(_FromAttrs):
    index: int
    url: str = Field(description="Short-lived signed GET for the original file")
    content_type: str
    size: int


class TouchedConcept(_FromAttrs):
    concept_id: str
    title: str
    is_new: bool


class ReadResponse(_FromAttrs):
    id: str
    source_id: str
    number: int = Field(description="1-based, oldest first")
    model_id: Optional[str]
    client_label: Optional[str] = Field(description="The MCP client that did the read")
    confidence: float
    low_confidence: bool
    status: Literal["active", "superseded"]
    created_at: datetime
    concepts_found: int
    questions_written: int
    concepts: list[TouchedConcept]


class ActiveReadResponse(ReadResponse):
    markdown: str


class SourceDetailResponse(SourceResponse):
    pages: list[SourcePageResponse]
    active_read: Optional[ActiveReadResponse]
    reads: list[ReadResponse] = Field(description="Every read, newest first")


class RereadRequest(BaseModel):
    preferred_model: Optional[str] = Field(
        default=None,
        max_length=64,
        description="'claude' or 'codex' (free text, kept as given); null lets the next "
        "client decide",
    )


class RereadResponse(BaseModel):
    source_id: str
    requested_at: datetime
    preferred_model: Optional[str]


class StorageResponse(_FromAttrs):
    bytes_used: int = Field(description="Bytes of every confirmed upload the user keeps")
    source_count: int
    page_count: int


class ExtractionResponse(BaseModel):
    id: str
    source_id: str
    confidence: float
    model_id: str | None
    status: str
    created_at: datetime


@router.get("", response_model=list[SourceResponse])
async def list_sources(
    user: UserDep,
    rt: RuntimeDep,
    track_id: Optional[str] = None,
    status: Optional[StatusLiteral] = None,
) -> list[SourceResponse]:
    sources = await SourceService(rt).list_sources(
        user.user_id,
        track_id=track_id,
        status=ProcessingStatus(status) if status else None,
    )
    return [SourceResponse.model_validate(s) for s in sources]


@router.get("/{source_id}", response_model=SourceDetailResponse, responses=NOT_FOUND)
async def get_source(source_id: str, user: UserDep, rt: RuntimeDep) -> SourceDetailResponse:
    try:
        detail = await SourceService(rt).detail(user.user_id, source_id)
    except SourceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return SourceDetailResponse.model_validate(detail)


@router.post("/{source_id}/reread", response_model=RereadResponse, responses=NOT_FOUND)
async def reread_source(
    source_id: str, user: UserDep, rt: RuntimeDep, body: RereadRequest | None = None
) -> RereadResponse:
    preferred = (body.preferred_model or "").strip() or None if body else None
    try:
        source = await SourceService(rt).request_reread(user.user_id, source_id, preferred)
    except SourceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return RereadResponse(
        source_id=source.id,
        requested_at=source.reread_requested_at,
        preferred_model=source.reread_preferred_model,
    )


@router.get(
    "/{source_id}/extractions", response_model=list[ExtractionResponse], responses=NOT_FOUND
)
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
            # The source's pointer is the authority on which read is active.
            status="active" if e.id == source.active_extraction_id else "superseded",
            created_at=e.created_at,
        )
        for e in history
    ]


@storage_router.get("", response_model=StorageResponse)
async def get_storage(user: UserDep, rt: RuntimeDep) -> StorageResponse:
    return StorageResponse.model_validate(await SourceService(rt).storage(user.user_id))
