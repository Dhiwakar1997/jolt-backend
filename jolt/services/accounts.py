"""Accounts, sign-in and token management (LLD §5, BE-03).

* Sign-in (Google, or the dev bypass) upserts the user by their identity and
  issues a fresh **app token** for that device.
* Sign-out revokes the app token that made the request.
* **MCP tokens** are created per connected client (plaintext returned once),
  listed by metadata only, and revoked by id.

Plaintext tokens are never stored: each `AuthToken` holds the Argon2 hash and the
HMAC lookup key (see `jolt/auth/tokens.py`).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from azure.cosmos import exceptions as cosmos_exc

from jolt.auth.google import GoogleIdentity
from jolt.auth.tokens import generate_token, hash_token, last4, lookup_key
from jolt.domain.models import AuthToken, TokenKind, User, user_id_for
from jolt.runtime import Runtime


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class IssuedToken:
    record: AuthToken
    token: str  # plaintext: returned exactly once, never stored


@dataclass
class SignedIn:
    user: User
    app_token: str


@dataclass
class Onboarding:
    has_track: bool
    has_mcp_token: bool
    first_sync_at: Optional[datetime]


@dataclass
class Me:
    user: User
    onboarding: Onboarding
    has_api_key: bool


class AccountError(Exception):
    pass


class TokenNotFound(AccountError):
    pass


class AccountService:
    def __init__(self, rt: Runtime) -> None:
        self._rt = rt

    # -- sign-in ---------------------------------------------------------------
    async def sign_in_google(self, identity: GoogleIdentity) -> SignedIn:
        user = await self._upsert_user(
            provider="google",
            subject=identity.sub,
            email=identity.email,
            display_name=identity.name,
            picture_url=identity.picture,
        )
        return SignedIn(user=user, app_token=(await self.issue_app_token(user.id)).token)

    async def sign_in_dev(self, email: str, display_name: Optional[str] = None) -> SignedIn:
        email = email.strip().lower()
        user = await self._upsert_user(
            provider="dev", subject=email, email=email, display_name=display_name
        )
        return SignedIn(user=user, app_token=(await self.issue_app_token(user.id)).token)

    async def _upsert_user(
        self,
        *,
        provider: str,
        subject: str,
        email: Optional[str],
        display_name: Optional[str],
        picture_url: Optional[str] = None,
    ) -> User:
        users = self._rt.repos.users
        user_id = user_id_for(provider, subject)
        existing = await users.get_user(user_id)
        if existing is None:
            fresh = User(
                id=user_id,
                email=email,
                display_name=display_name,
                picture_url=picture_url,
                auth_provider=provider,
                auth_subject=subject,
                last_sign_in_at=_now(),
            )
            try:
                return await users.create(fresh)
            except cosmos_exc.CosmosResourceExistsError:
                # Two first sign-ins raced (two devices); the other one created it.
                existing = await users.get_user(user_id)
                if existing is None:  # pragma: no cover - deleted in between
                    raise
        # Refresh the profile from the identity provider on every sign-in.
        existing.email = email or existing.email
        existing.display_name = display_name or existing.display_name
        existing.picture_url = picture_url or existing.picture_url
        existing.last_sign_in_at = _now()
        return await users.upsert(existing)

    # -- tokens ----------------------------------------------------------------
    async def _issue(self, user_id: str, kind: TokenKind, label: Optional[str]) -> IssuedToken:
        settings = self._rt.settings
        token = generate_token(kind, settings)
        record = AuthToken(
            user_id=user_id,
            kind=kind,
            lookup=lookup_key(token, settings.token_hash_pepper),
            # Argon2 is CPU-bound; argon2-cffi releases the GIL.
            token_hash=await asyncio.to_thread(hash_token, token, settings.token_hash_pepper),
            label=label,
            last4=last4(token),
        )
        created = await self._rt.repos.tokens.create(record)
        return IssuedToken(record=created, token=token)

    async def issue_app_token(self, user_id: str) -> IssuedToken:
        return await self._issue(user_id, TokenKind.APP, label=None)

    async def sign_out(self, user_id: str, token_id: str) -> None:
        """Revoke the app token that made the request."""
        await self._rt.repos.tokens.revoke(user_id, token_id)

    async def create_mcp_token(self, user_id: str, label: str) -> IssuedToken:
        return await self._issue(user_id, TokenKind.MCP, label=label)

    async def list_mcp_tokens(self, user_id: str) -> list[AuthToken]:
        return await self._rt.repos.tokens.list_for_user(user_id, TokenKind.MCP)

    async def revoke_mcp_token(self, user_id: str, token_id: str) -> None:
        tokens = self._rt.repos.tokens
        record = await tokens.get_token(user_id, token_id)
        if record is None or record.kind != TokenKind.MCP:
            raise TokenNotFound("token not found")
        await tokens.revoke(user_id, token_id)

    # -- /v1/me ----------------------------------------------------------------
    async def me(self, user: User) -> Me:
        repos = self._rt.repos
        # An own track, or an enrolment in a curated one (BE-09).
        tracks = user.enrolments or await repos.tracks.list_for_user(user.id)
        mcp_tokens = await repos.tokens.list_for_user(user.id, TokenKind.MCP)
        api_keys = await repos.api_keys.list_for_user(user.id)
        return Me(
            user=user,
            onboarding=Onboarding(
                has_track=bool(tracks),
                has_mcp_token=bool(mcp_tokens),
                first_sync_at=user.first_sync_at,
            ),
            has_api_key=bool(api_keys),
        )
