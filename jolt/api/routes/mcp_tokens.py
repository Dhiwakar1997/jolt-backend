"""MCP token management (BE-03).

One MCP token (`jolt_live_…`) per connected Claude / Codex client. The plaintext
is returned once, by `POST`; listing shows metadata only; `DELETE` revokes, and
the next MCP call with that token fails.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field, field_validator

from jolt.api.deps import RuntimeDep, UserDep
from jolt.api.errors import NOT_FOUND, UNAUTHORIZED
from jolt.domain.models import AuthToken
from jolt.services.accounts import AccountService, TokenNotFound

router = APIRouter(prefix="/v1/mcp-tokens", tags=["mcp-tokens"], responses=UNAUTHORIZED)


class CreateMcpTokenRequest(BaseModel):
    label: str = Field(
        min_length=1, max_length=60, description='Which client it is for, e.g. "Claude Desktop"'
    )

    @field_validator("label")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("label must not be blank")
        return v


class McpTokenResponse(BaseModel):
    id: str
    label: str
    last4: str
    created_at: datetime
    last_used_at: datetime | None


class CreatedMcpTokenResponse(McpTokenResponse):
    token: str = Field(description="The plaintext token. Shown only in this response.")


def _out(record: AuthToken) -> McpTokenResponse:
    return McpTokenResponse(
        id=record.id,
        label=record.label or "",
        last4=record.last4,
        created_at=record.created_at,
        last_used_at=record.last_used_at,
    )


@router.post("", status_code=201, response_model=CreatedMcpTokenResponse)
async def create_mcp_token(
    body: CreateMcpTokenRequest, user: UserDep, rt: RuntimeDep
) -> CreatedMcpTokenResponse:
    issued = await AccountService(rt).create_mcp_token(user.user_id, body.label)
    return CreatedMcpTokenResponse(**_out(issued.record).model_dump(), token=issued.token)


@router.get("", response_model=list[McpTokenResponse])
async def list_mcp_tokens(user: UserDep, rt: RuntimeDep) -> list[McpTokenResponse]:
    """The caller's MCP tokens, newest first. Never includes the token itself."""
    return [_out(r) for r in await AccountService(rt).list_mcp_tokens(user.user_id)]


@router.delete("/{token_id}", status_code=204, responses=NOT_FOUND)
async def revoke_mcp_token(token_id: str, user: UserDep, rt: RuntimeDep) -> Response:
    try:
        await AccountService(rt).revoke_mcp_token(user.user_id, token_id)
    except TokenNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(status_code=204)
