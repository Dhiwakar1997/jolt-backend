"""Settings routes — retention prefs and per-user API keys (LLD §5.1, §7.6)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from jolt.api.deps import RuntimeDep, UserDep
from jolt.services.credentials import CredentialError, CredentialService

router = APIRouter(prefix="/v1/settings", tags=["settings"])


class ApiKeyRequest(BaseModel):
    provider: str
    key: str


class ApiKeyResponse(BaseModel):
    provider: str
    last4: str
    status: str


class RetentionRequest(BaseModel):
    retention_days: int | None = None


@router.post("/api-key", response_model=ApiKeyResponse)
async def set_api_key(body: ApiKeyRequest, user: UserDep, rt: RuntimeDep) -> ApiKeyResponse:
    try:
        stored = await CredentialService(rt).capture(user.user_id, body.provider, body.key)
    except ValueError as exc:  # unsupported provider
        raise HTTPException(status_code=400, detail=str(exc))
    except CredentialError as exc:  # failed validation
        raise HTTPException(status_code=422, detail=str(exc))
    return ApiKeyResponse(provider=stored.provider, last4=stored.last4, status=stored.status)


@router.get("/api-key", response_model=list[ApiKeyResponse])
async def list_api_keys(user: UserDep, rt: RuntimeDep) -> list[ApiKeyResponse]:
    keys = await CredentialService(rt).list_keys(user.user_id)
    return [ApiKeyResponse(provider=k.provider, last4=k.last4, status=k.status) for k in keys]


@router.delete("/api-key/{provider}", status_code=204)
async def delete_api_key(provider: str, user: UserDep, rt: RuntimeDep) -> Response:
    try:
        await CredentialService(rt).delete(user.user_id, provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return Response(status_code=204)


@router.put("/retention")
async def set_retention(body: RetentionRequest, user: UserDep, rt: RuntimeDep) -> dict:
    u = user.user
    u.retention_days = body.retention_days
    await rt.repos.users.upsert(u)
    return {"retention_days": body.retention_days}
