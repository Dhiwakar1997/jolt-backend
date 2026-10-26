"""`GET /v1/me` — the signed-in user and where they are in onboarding (BE-03).

The app calls it on launch to decide where a returning user resumes, and to show
the MCP server address on "Connect your model".
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from jolt.api.deps import RuntimeDep, UserDep
from jolt.api.errors import UNAUTHORIZED
from jolt.api.routes.auth import UserResponse, user_response
from jolt.config import Settings
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
