"""`/v1/me` — the signed-in user, onboarding (BE-03) and data rights (BE-08).

`GET /v1/me`: the app calls it on launch to decide where a returning user
resumes, and to show the MCP server address on "Connect your model".
`POST /v1/export` and `DELETE /v1/me`: Settings · Data (export everything,
delete the account).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from jolt.api.deps import RuntimeDep, UserDep
from jolt.api.errors import UNAUTHORIZED
from jolt.api.routes.auth import UserResponse, user_response
from jolt.config import Settings
from jolt.services.account_data import AccountDataService
from jolt.services.accounts import AccountService

router = APIRouter(prefix="/v1", tags=["me"], responses=UNAUTHORIZED)


class OnboardingResponse(BaseModel):
    has_track: bool
    has_mcp_token: bool
    first_sync_at: datetime | None = Field(
        description="When the first nightly sync succeeded; null until then (set by BE-04)"
    )


class MeResponse(BaseModel):
    user: UserResponse
    onboarding: OnboardingResponse
    mcp_server_url: str = Field(description="The address to paste into Claude / Codex")
    has_api_key: bool = Field(description="True when the user stored a provider API key")


def mcp_server_url(settings: Settings, request: Request) -> str:
    """MCP_SERVER_URL, else <PUBLIC_BASE_URL>/mcp/sse, else this request's base URL."""
    if settings.mcp_server_url:
        return settings.mcp_server_url
    base = settings.public_base_url or str(request.base_url)
    return f"{base.rstrip('/')}/mcp/sse"


@router.get("/me", response_model=MeResponse)
async def get_me(user: UserDep, rt: RuntimeDep, request: Request) -> MeResponse:
    me = await AccountService(rt).me(user.user)
    return MeResponse(
        user=user_response(me.user),
        onboarding=OnboardingResponse(
            has_track=me.onboarding.has_track,
            has_mcp_token=me.onboarding.has_mcp_token,
            first_sync_at=me.onboarding.first_sync_at,
        ),
        mcp_server_url=mcp_server_url(rt.settings, request),
        has_api_key=me.has_api_key,
    )


class ExportResponse(BaseModel):
    download_url: str = Field(
        description="Signed GET URL of the JSON bundle (served as an attachment)"
    )
    expires_at: datetime = Field(description="When the URL stops working")
    bytes: int = Field(description="Size of the bundle")


@router.post("/export", response_model=ExportResponse)
async def export_data(user: UserDep, rt: RuntimeDep) -> ExportResponse:
    """Everything Jolt keeps for the user as one JSON file: profile, settings,
    tracks, concepts, questions (every version), FSRS states, reviews, gradings,
    review and study sessions, sources metadata with every read (markdown
    included), sync runs, MCP tokens and API keys (metadata only, never secrets).
    A new export replaces the previous file."""
    result = await AccountDataService(rt).export(user.user_id)
    return ExportResponse(
        download_url=result.download_url, expires_at=result.expires_at, bytes=result.bytes
    )


class DeleteAccountRequest(BaseModel):
    confirm: Literal["DELETE"] = Field(description='Must be the literal string "DELETE"')


@router.delete("/me", status_code=204)
async def delete_account(body: DeleteAccountRequest, user: UserDep, rt: RuntimeDep) -> Response:
    """Delete the account and everything under it, irreversibly: Key Vault
    secrets, uploaded files and exports, every document, the tokens (every
    device is signed out, every connected client cut off), then the user."""
    await AccountDataService(rt).delete_account(user.user_id)
    return Response(status_code=204)
