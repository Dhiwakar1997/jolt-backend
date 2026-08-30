"""Account routes — create an account, issue the opaque token (LLD §5).

The plaintext token is returned exactly once here. There is no endpoint to read it
back; only its Argon2 hash is stored.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from jolt.api.deps import RuntimeDep
from jolt.services.accounts import AccountService

router = APIRouter(prefix="/v1/accounts", tags=["accounts"])


class CreateAccountRequest(BaseModel):
    display_name: str | None = None


class CreateAccountResponse(BaseModel):
    user_id: str
    token: str  # shown once — place it in the MCP client config / app secure storage


@router.post("", response_model=CreateAccountResponse)
async def create_account(body: CreateAccountRequest, rt: RuntimeDep) -> CreateAccountResponse:
    account = await AccountService(rt).create_account(body.display_name)
    return CreateAccountResponse(user_id=account.user_id, token=account.token)
