"""Upload routes (LLD §7.1)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from jolt.api.deps import RuntimeDep, UserDep
from jolt.services.uploads import UploadError, UploadService

router = APIRouter(prefix="/v1/uploads", tags=["uploads"])


class UploadRequest(BaseModel):
    filename: str
    content_type: str
    sha256: str


class UploadResponse(BaseModel):
    source_id: str
    upload_url: str


class ConfirmRequest(BaseModel):
    sha256: str | None = None


@router.post("/request", response_model=UploadResponse)
async def request_upload(body: UploadRequest, user: UserDep, rt: RuntimeDep) -> UploadResponse:
    ticket = await UploadService(rt).request_upload(
        user.user_id, body.filename, body.content_type, body.sha256
    )
    return UploadResponse(source_id=ticket.source_id, upload_url=ticket.upload_url)


@router.post("/{source_id}/confirm")
async def confirm_upload(
    source_id: str, body: ConfirmRequest, user: UserDep, rt: RuntimeDep
) -> dict:
    try:
        await UploadService(rt).confirm_upload(user.user_id, source_id, body.sha256)
    except UploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "ready"}
