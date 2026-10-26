"""Upload routes (LLD §7.1, BE-05).

A note of N pages is one source with N files (decision A7): the request lists
every file and gets one signed PUT URL per file; confirm checks each file's size
and SHA-256. The single-file fields (`filename`, `content_type`, `sha256`) still
work and mean one page.
"""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, model_validator

from jolt.api.deps import RuntimeDep, UserDep
from jolt.api.errors import BAD_REQUEST, NOT_FOUND, UNAUTHORIZED
from jolt.services.uploads import (
    ConfirmPage,
    PageSpec,
    UploadError,
    UploadInvalid,
    UploadService,
    UploadTrackNotFound,
)

router = APIRouter(prefix="/v1/uploads", tags=["uploads"], responses=UNAUTHORIZED)

SHA256_PATTERN = r"^[0-9a-fA-F]{64}$"


class UploadPage(BaseModel):
    """One file of the upload."""

    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(
        description="image/jpeg, image/png, image/heic, image/webp or application/pdf"
    )
    size: int = Field(ge=1, description="Bytes; at most `max_bytes_per_file`")
    sha256: str = Field(pattern=SHA256_PATTERN, description="Hex SHA-256 of the file")


class UploadRequest(BaseModel):
    """Either `pages` (one entry per file) or the single-file fields."""

    track_id: Optional[str] = Field(
        default=None, description="The caller's track the note belongs to"
    )
    title: Optional[str] = Field(
        default=None, max_length=200, description="Defaults to the first filename"
    )
    filename: Optional[str] = Field(default=None, min_length=1, max_length=255)
    content_type: Optional[str] = None
    sha256: Optional[str] = Field(default=None, pattern=SHA256_PATTERN)
    size: Optional[int] = Field(
        default=None, ge=1, description="Single file: its bytes. Ignored when `pages` is sent."
    )
    pages: Optional[list[UploadPage]] = Field(
        default=None, min_length=1, description="Files of a multi-page note, in page order"
    )

    @model_validator(mode="after")
    def _one_shape(self) -> "UploadRequest":
        if self.pages:
            return self
        missing = [f for f in ("filename", "content_type", "sha256") if not getattr(self, f)]
        if missing:
            raise ValueError(
                "send `pages`, or `filename`, `content_type` and `sha256` for one file "
                f"(missing: {', '.join(missing)})"
            )
        return self

    def page_specs(self) -> tuple[list[PageSpec], tuple]:
        if self.pages:
            specs = [PageSpec(p.filename, p.content_type, p.sha256, p.size) for p in self.pages]
            return specs, ("body", "pages")
        spec = PageSpec(self.filename or "", self.content_type or "", self.sha256 or "", self.size)
        return [spec], ("body",)


class UploadPageTicket(BaseModel):
    index: int
    upload_url: str


class UploadResponse(BaseModel):
    source_id: str
    upload_url: str = Field(description="The first page's PUT URL")
    pages: list[UploadPageTicket] = Field(description="One signed PUT URL per page")
    max_bytes_per_file: int
    accepted_types: list[str]


class ConfirmPageCheck(BaseModel):
    index: int = Field(ge=0)
    sha256: str = Field(pattern=SHA256_PATTERN)
    size: int = Field(ge=1)


class ConfirmRequest(BaseModel):
    sha256: str | None = Field(default=None, description="The first page's hash (single file)")
    pages: Optional[list[ConfirmPageCheck]] = Field(
        default=None, description="Optional: what the client uploaded, checked against the request"
    )


class ConfirmResponse(BaseModel):
    status: Literal["ready"]


@router.post(
    "/request",
    response_model=UploadResponse,
    responses={**NOT_FOUND},
)
async def request_upload(body: UploadRequest, user: UserDep, rt: RuntimeDep) -> UploadResponse:
    specs, loc = body.page_specs()
    try:
        ticket = await UploadService(rt).request_upload(
            user.user_id, specs, track_id=body.track_id, title=body.title, loc_prefix=loc
        )
    except UploadTrackNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except UploadInvalid as exc:
        # Same 422 body as a schema failure, pointing at the offending field.
        raise RequestValidationError([{"loc": exc.loc, "msg": exc.msg, "type": "value_error"}])
    return UploadResponse(
        source_id=ticket.source_id,
        upload_url=ticket.upload_url,
        pages=[UploadPageTicket(index=p.index, upload_url=p.upload_url) for p in ticket.pages],
        max_bytes_per_file=ticket.max_bytes_per_file,
        accepted_types=ticket.accepted_types,
    )


@router.post("/{source_id}/confirm", response_model=ConfirmResponse, responses=BAD_REQUEST)
async def confirm_upload(
    source_id: str, body: ConfirmRequest, user: UserDep, rt: RuntimeDep
) -> ConfirmResponse:
    checks = [ConfirmPage(p.index, p.sha256, p.size) for p in body.pages or []]
    try:
        await UploadService(rt).confirm_upload(user.user_id, source_id, body.sha256, checks)
    except UploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return ConfirmResponse(status="ready")
