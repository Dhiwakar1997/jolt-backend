"""Per-user provider API-key capture and deletion (LLD §5.1, §7.6).

Validate-at-capture, store raw key in Key Vault, write only a reference to Cosmos.
Deletion purges the Key Vault secret first, then the Cosmos reference, so a crash
never leaves a dangling reference to a missing secret.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from jolt.credentials.providers import get_adapter
from jolt.domain.models import ApiKeyRef
from jolt.runtime import Runtime


class CredentialError(Exception):
    pass


@dataclass
class StoredKey:
    provider: str
    last4: str
    status: str


class CredentialService:
    def __init__(self, rt: Runtime) -> None:
        self._rt = rt

    async def capture(self, user_id: str, provider: str, key: str) -> StoredKey:
        adapter = get_adapter(provider)  # raises ValueError on unsupported provider
        result = await adapter.validate(key)
        if not result.ok:
            # Reject at capture; store nothing (LLD §5.1).
            raise CredentialError(f"key validation failed: {result.detail}")

        key_ref = await self._rt.vault.set_key(user_id, adapter.name, key)

        existing = await self._rt.repos.api_keys.for_provider(user_id, adapter.name)
        now = datetime.now(timezone.utc)
        if existing:
            existing.key_ref = key_ref
            existing.last4 = result.last4
            existing.last_validated_at = now
            await self._rt.repos.api_keys.upsert(existing)
        else:
            ref = ApiKeyRef(
                user_id=user_id,
                provider=adapter.name,
                key_ref=key_ref,
                last4=result.last4,
                last_validated_at=now,
            )
            await self._rt.repos.api_keys.create(ref)

        return StoredKey(provider=adapter.name, last4=result.last4, status="validated")

    async def delete(self, user_id: str, provider: str) -> None:
        adapter = get_adapter(provider)
        # Vault first, then Cosmos reference — order matters (LLD §5.1).
        await self._rt.vault.delete_key(user_id, adapter.name)
        existing = await self._rt.repos.api_keys.for_provider(user_id, adapter.name)
        if existing:
            await self._rt.repos.api_keys.delete(existing.id, user_id)

    async def list_keys(self, user_id: str) -> list[StoredKey]:
        refs = await self._rt.repos.api_keys.list_for_user(user_id)
        return [StoredKey(provider=r.provider, last4=r.last4, status="stored") for r in refs]
