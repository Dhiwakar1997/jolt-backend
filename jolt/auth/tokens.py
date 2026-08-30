"""Opaque token issue / hash / verify (LLD §5).

Tokens are `jolt_live_<random>`. Only an Argon2 hash is stored; the plaintext is
shown once at issue time. A server-side pepper (from config / Key Vault) is mixed
in so a database leak alone cannot mount an offline attack with a stolen hash.
"""

from __future__ import annotations

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError

from jolt.config import get_settings

_ph = PasswordHasher()


def _peppered(plaintext: str) -> str:
    return f"{plaintext}{get_settings().token_hash_pepper}"


def generate_token() -> str:
    """Return a fresh plaintext token. Shown to the user exactly once."""
    prefix = get_settings().token_prefix
    return f"{prefix}{secrets.token_urlsafe(32)}"


def hash_token(plaintext: str) -> str:
    """Argon2 hash of the peppered token, safe to persist."""
    return _ph.hash(_peppered(plaintext))


def verify_token(plaintext: str, stored_hash: str) -> bool:
    """Constant-time-ish verification against a stored Argon2 hash."""
    try:
        return _ph.verify(stored_hash, _peppered(plaintext))
    except (VerifyMismatchError, VerificationError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """True if the stored hash used weaker parameters than the current policy."""
    return _ph.check_needs_rehash(stored_hash)
