"""ApiKeyRef repository (LLD §5.1). Holds references only — no key material.

Partition key = /user_id.
"""

from __future__ import annotations

from typing import Optional

from jolt.data.repositories.base import Repository
from jolt.domain.models import ApiKeyRef


class ApiKeyRefRepository(Repository[ApiKeyRef]):
    container_name = "api_keys"
    model = ApiKeyRef

    async def for_provider(self, user_id: str, provider: str) -> Optional[ApiKeyRef]:
        rows = await self._query(
            "SELECT * FROM c WHERE c.provider = @p",
            [{"name": "@p", "value": provider}],
            partition_value=user_id,
        )
        return rows[0] if rows else None

    async def list_for_user(self, user_id: str) -> list[ApiKeyRef]:
        return await self._query("SELECT * FROM c", [], partition_value=user_id)
