"""Resolve token → user, scope enforcement (LLD §5, BE-03).

Framework-agnostic: `authenticate` takes the repositories, the pepper, a plaintext
token and the token kind the calling surface accepts, and returns a UserContext.
The REST dependency (app tokens) and the MCP auth path (MCP tokens) share it. The
`user_id` it yields is what every repository call downstream requires.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from jolt.auth.tokens import lookup_key, verify_token
from jolt.data.repositories.tokens import TokensRepository
from jolt.data.repositories.users import UsersRepository
from jolt.domain.models import AuthToken, TokenKind, User

logger = logging.getLogger("jolt.auth")

# `last_used_at` is written at most this often per token, so a busy client does
# not turn every request into a write.
LAST_USED_RESOLUTION = timedelta(seconds=60)


@dataclass(frozen=True)
class UserContext:
    user_id: str
    user: User
    token_id: str
    token_kind: TokenKind
    # The token's label ("Claude Desktop"); names the client on sync runs (BE-04).
    token_label: Optional[str] = None


class AuthError(Exception):
    """The token cannot be resolved to a user on this surface. The message is
    safe to return to the caller."""


async def authenticate(
    token: Optional[str],
    *,
    kind: TokenKind,
    tokens: TokensRepository,
    users: UsersRepository,
    pepper: str,
    idle_timeout: Optional[timedelta] = None,
) -> UserContext:
    """Return the UserContext for a valid token of `kind`, or raise AuthError.

    One indexed lookup (HMAC of the token) and one Argon2 verify; no scan. A
    revoked token's document is gone, so revocation takes effect on the next
    request. With `idle_timeout` (app tokens, `APP_TOKEN_IDLE_DAYS`), a token
    unused for longer is deleted and refused as expired (BE-10, GAPS #7).
    """
    if not token:
        raise AuthError("missing bearer token")
    record = await tokens.find_by_lookup(lookup_key(token, pepper))
    # Argon2 is CPU-bound; argon2-cffi releases the GIL, so verify off the loop.
    if record is None or not await asyncio.to_thread(
        verify_token, token, record.token_hash, pepper
    ):
        raise AuthError("invalid token")
    if record.kind != kind:
        surface = "/v1 accepts app tokens" if kind == TokenKind.APP else "/mcp accepts MCP tokens"
        raise AuthError(f"wrong token kind: {surface} only")
    if idle_timeout is not None and is_idle(record, idle_timeout):
        try:
            await tokens.revoke(record.user_id, record.id)
        except Exception:  # already gone; refuse it either way
            logger.debug("could not delete expired token %s", record.id, exc_info=True)
        raise AuthError("token expired; sign in again")
    user = await users.get_user(record.user_id)
    if user is None:
        raise AuthError("invalid token")
    await _touch(record, tokens)
    return UserContext(
        user_id=user.id,
        user=user,
        token_id=record.id,
        token_kind=record.kind,
        token_label=record.label,
    )


def is_idle(record: AuthToken, idle_timeout: timedelta, now: Optional[datetime] = None) -> bool:
    """True when the token has not been used (or, never used, issued) within
    `idle_timeout`. `last_used_at` is written at most once a minute, so the
    timeout is exact to `LAST_USED_RESOLUTION`."""
    now = now or datetime.now(timezone.utc)
    last = record.last_used_at or record.created_at
    return now - last > idle_timeout


async def _touch(record: AuthToken, tokens: TokensRepository) -> None:
    """Best-effort `last_used_at` update; never fails the request."""
    now = datetime.now(timezone.utc)
    if record.last_used_at is not None and now - record.last_used_at < LAST_USED_RESOLUTION:
        return
    record.last_used_at = now
    try:
        await tokens.replace(record)  # etag-guarded; a lost race just skips
    except Exception:  # e.g. revoked concurrently
        logger.debug("could not update last_used_at for token %s", record.id, exc_info=True)


def bearer_from_header(authorization: Optional[str]) -> Optional[str]:
    """Extract the token from an `Authorization: Bearer <token>` header."""
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    # Tolerate a bare token (some MCP clients send the raw value).
    return authorization.strip()
