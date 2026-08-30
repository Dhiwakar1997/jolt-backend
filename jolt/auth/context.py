"""Resolve token → user, scope enforcement (LLD §5).

Framework-agnostic: this takes a UsersRepository and a plaintext token and returns
a UserContext, so both the REST dependency and the MCP auth path share one code
path. The `user_id` it yields is what every repository call downstream requires.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from jolt.auth.tokens import verify_token
from jolt.data.repositories.users import UsersRepository
from jolt.domain.models import User


@dataclass(frozen=True)
class UserContext:
    user_id: str
    user: User


class AuthError(Exception):
    """Raised when a token cannot be resolved to a user."""


async def resolve_token(token: str, users: UsersRepository) -> Optional[UserContext]:
    """Return the UserContext for a valid token, or None.

    The token is opaque and carries no user id, so resolution verifies the
    plaintext against candidate Argon2 hashes. This is the one pre-auth path that
    runs without a user_id in hand — it is what establishes it.
    """
    if not token:
        return None
    for candidate in await users.find_by_token_hash_candidates():
        if verify_token(token, candidate.token_hash):
            return UserContext(user_id=candidate.id, user=candidate)
    return None


def bearer_from_header(authorization: Optional[str]) -> Optional[str]:
    """Extract the token from an `Authorization: Bearer <token>` header."""
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    # Tolerate a bare token (some MCP clients send the raw value).
    return authorization.strip()
