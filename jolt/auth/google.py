"""Google ID token verification for sign-in (BE-03).

The app gets an ID token from Google Identity Services and posts it to
`POST /v1/auth/google`. The server verifies it itself, without trusting the
client: RS256 signature against Google's published keys (JWKS), issuer
(`accounts.google.com`), audience (one of `GOOGLE_OAUTH_CLIENT_ID`), expiry,
`iat` / `nbf` not in the future (30 s leeway), and a verified email.

Keys come from a `KeySource`. Production uses `GoogleJwks`, which fetches
https://www.googleapis.com/oauth2/v3/certs and caches it for the response's
`Cache-Control: max-age`, refetching early only for an unknown key id. Tests use
`StaticKeys` with their own RSA key and mint their own tokens, so they never
touch the network.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any, Optional, Protocol

import httpx
import jwt

GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"


class GoogleTokenError(Exception):
    """The ID token is not acceptable (bad signature, audience, issuer, expired…)."""


class GoogleSignInNotConfigured(Exception):
    """GOOGLE_OAUTH_CLIENT_ID is not set."""


class GoogleKeysUnavailable(Exception):
    """Google's signing keys could not be fetched."""


@dataclass(frozen=True)
class GoogleIdentity:
    sub: str
    email: Optional[str]
    email_verified: bool
    name: Optional[str]
    picture: Optional[str]


class KeySource(Protocol):
    async def get_key(self, kid: str) -> Any:
        """Return the public key for `kid`, or raise GoogleTokenError."""


class StaticKeys:
    """A fixed set of public keys by key id (tests, or pinned keys)."""

    def __init__(self, keys: dict[str, Any]) -> None:
        self._keys = dict(keys)

    async def get_key(self, kid: str) -> Any:
        try:
            return self._keys[kid]
        except KeyError as exc:
            raise GoogleTokenError("unknown signing key") from exc


_MAX_AGE_RE = re.compile(r"max-age=(\d+)")


class GoogleJwks:
    """Google's JWKS, fetched lazily and cached per `Cache-Control: max-age`."""

    def __init__(
        self,
        url: str = GOOGLE_JWKS_URL,
        *,
        default_ttl: float = 3600,
        min_refetch_interval: float = 60,
        timeout: float = 10,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = url
        self._default_ttl = default_ttl
        self._min_refetch = min_refetch_interval
        self._timeout = timeout
        self._transport = transport
        self._keys: dict[str, Any] = {}
        self._expires_at = 0.0
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    async def get_key(self, kid: str) -> Any:
        now = time.monotonic()
        if kid in self._keys and now < self._expires_at:
            return self._keys[kid]
        async with self._lock:
            now = time.monotonic()
            stale = now >= self._expires_at
            unknown = kid not in self._keys and now - self._fetched_at >= self._min_refetch
            if stale or unknown:
                await self._refresh()
        if kid not in self._keys:
            raise GoogleTokenError("unknown signing key")
        return self._keys[kid]

    async def _refresh(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as c:
                resp = await c.get(self._url)
                resp.raise_for_status()
                data = resp.json()
            keyset = jwt.PyJWKSet.from_dict(data)
        except (httpx.HTTPError, ValueError, jwt.PyJWKSetError) as exc:
            if self._keys:  # keep serving the keys we have
                return
            raise GoogleKeysUnavailable(f"could not fetch Google signing keys: {exc}") from exc
        match = _MAX_AGE_RE.search(resp.headers.get("cache-control", ""))
        ttl = float(match.group(1)) if match else self._default_ttl
        now = time.monotonic()
        self._keys = {k.key_id: k.key for k in keyset.keys if k.key_id}
        self._fetched_at = now
        self._expires_at = now + ttl


class GoogleIdTokenVerifier:
    """Verifies Google ID tokens for a set of OAuth client ids."""

    def __init__(
        self,
        client_ids: list[str],
        keys: KeySource | None = None,
        *,
        leeway_seconds: float = 30,
    ) -> None:
        self.client_ids = list(client_ids)
        self._keys = keys or GoogleJwks()
        self._leeway = leeway_seconds

    @property
    def configured(self) -> bool:
        return bool(self.client_ids)

    async def verify(self, id_token: str) -> GoogleIdentity:
        if not self.configured:
            raise GoogleSignInNotConfigured("Google sign-in is not configured on this server")
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.InvalidTokenError as exc:
            raise GoogleTokenError("malformed token") from exc
        if header.get("alg") != "RS256":
            raise GoogleTokenError("unexpected signing algorithm")
        kid = header.get("kid")
        if not kid:
            raise GoogleTokenError("token has no key id")
        key = await self._keys.get_key(kid)
        try:
            claims = jwt.decode(
                id_token,
                key,
                algorithms=["RS256"],
                audience=self.client_ids,
                issuer=GOOGLE_ISSUERS,
                leeway=self._leeway,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise GoogleTokenError("token expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise GoogleTokenError("token was issued for another client") from exc
        except jwt.InvalidIssuerError as exc:
            raise GoogleTokenError("token was not issued by Google") from exc
        except jwt.InvalidSignatureError as exc:
            raise GoogleTokenError("bad signature") from exc
        except jwt.InvalidTokenError as exc:
            raise GoogleTokenError(f"invalid token: {exc}") from exc
        # The account is keyed on `sub`, but the email is shown, exported and
        # used as the display-name fallback: refuse one Google has not
        # verified (BE-10 security review). Google sends the claim as a bool;
        # older tokens used the string "true".
        email = claims.get("email")
        verified = claims.get("email_verified", False) in (True, "true")
        if email and not verified:
            raise GoogleTokenError("the Google account's email address is not verified")
        return GoogleIdentity(
            sub=str(claims["sub"]),
            email=email,
            email_verified=verified,
            name=claims.get("name"),
            picture=claims.get("picture"),
        )
