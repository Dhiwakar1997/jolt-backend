"""Account creation — issue the per-user opaque token (LLD §5)."""

from __future__ import annotations

from dataclasses import dataclass

from jolt.auth.tokens import generate_token, hash_token
from jolt.domain.models import User
from jolt.runtime import Runtime


@dataclass
class NewAccount:
    user_id: str
    token: str  # plaintext — returned exactly once, never stored


class AccountService:
    def __init__(self, rt: Runtime) -> None:
        self._rt = rt

    async def create_account(self, display_name: str | None = None) -> NewAccount:
        token = generate_token()
        user = User(token_hash=hash_token(token), display_name=display_name)
        created = await self._rt.repos.users.create(user)
        return NewAccount(user_id=created.id, token=token)
