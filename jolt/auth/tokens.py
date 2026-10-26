"""Opaque token issue / hash / look up / verify (LLD §5, BE-03).

Two kinds share one scheme:

* app tokens, `jolt_app_<random>`: one per signed-in device, accepted on `/v1`;
* MCP tokens, `jolt_live_<random>`: one per connected client, accepted on `/mcp`.

Only an Argon2 hash of the peppered token is stored; the plaintext is shown once
at issue time. Argon2 salts every hash, so a token cannot be found by its hash.
To avoid walking every hash, each token document also stores a *lookup key*: a
truncated HMAC-SHA256 of the token keyed by the same server-side pepper. It is an
indexed, non-secret field: resolution queries it, then verifies the Argon2 hash of
the single match. Without the pepper a database leak gives neither a way to test
guesses offline (Argon2 + pepper) nor to map a token to its document (HMAC).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from jolt.config import Settings
from jolt.domain.models import TokenKind

_ph = PasswordHasher()

# 32 hex chars = 128 bits of the HMAC: collisions are negligible, and a
# collision would still fail the Argon2 check.
LOOKUP_HEX_CHARS = 32
_LOOKUP_DOMAIN = b"jolt-token-lookup:v1:"


def _peppered(plaintext: str, pepper: str) -> str:
    return f"{plaintext}{pepper}"


def prefix_for(kind: TokenKind, settings: Settings) -> str:
    return settings.app_token_prefix if kind == TokenKind.APP else settings.mcp_token_prefix


def generate_token(kind: TokenKind, settings: Settings) -> str:
    """Return a fresh plaintext token. Shown to the user exactly once."""
    return f"{prefix_for(kind, settings)}{secrets.token_urlsafe(32)}"


def lookup_key(plaintext: str, pepper: str) -> str:
    """Indexed lookup key for a token: truncated HMAC-SHA256 keyed by the pepper."""
    mac = hmac.new(pepper.encode(), _LOOKUP_DOMAIN + plaintext.encode(), hashlib.sha256)
    return mac.hexdigest()[:LOOKUP_HEX_CHARS]


def hash_token(plaintext: str, pepper: str) -> str:
    """Argon2 hash of the peppered token, safe to persist."""
    return _ph.hash(_peppered(plaintext, pepper))


def verify_token(plaintext: str, stored_hash: str, pepper: str) -> bool:
    """Verify a plaintext token against its stored Argon2 hash."""
    try:
        return _ph.verify(stored_hash, _peppered(plaintext, pepper))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def last4(plaintext: str) -> str:
    return plaintext[-4:]


def needs_rehash(stored_hash: str) -> bool:
    """True if the stored hash used weaker parameters than the current policy."""
    return _ph.check_needs_rehash(stored_hash)
