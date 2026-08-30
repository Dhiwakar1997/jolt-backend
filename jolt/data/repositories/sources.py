"""Sources and Extractions repositories (LLD §6, §7).

Sources partition by /user_id; extractions by /source_id (history read together).
"""

from __future__ import annotations

from typing import Optional

from jolt.data.repositories.base import Repository
from jolt.domain.models import (
    Extraction,
    ExtractionStatus,
    ProcessingStatus,
    Source,
)


class SourcesRepository(Repository[Source]):
    container_name = "sources"
    model = Source

    async def get_source(self, source_id: str, user_id: str) -> Optional[Source]:
        return await self.get(source_id, partition_value=user_id)

    async def list_for_user(self, user_id: str) -> list[Source]:
        return await self._query(
            "SELECT * FROM c ORDER BY c.created_at DESC", [], partition_value=user_id
        )

    async def list_unprocessed(self, user_id: str, limit: int) -> list[Source]:
        """Claimable sources: unprocessed, or in_flight with an expired lease.

        `now` is compared in the app rather than the query so the same predicate
        is reused by the lease layer.
        """
        query = (
            "SELECT * FROM c WHERE c.processing_status IN (@unproc, @inflight) "
            "ORDER BY c.created_at ASC OFFSET 0 LIMIT @limit"
        )
        params = [
            {"name": "@unproc", "value": ProcessingStatus.UNPROCESSED.value},
            {"name": "@inflight", "value": ProcessingStatus.IN_FLIGHT.value},
            {"name": "@limit", "value": limit},
        ]
        return await self._query(query, params, partition_value=user_id)


class ExtractionsRepository(Repository[Extraction]):
    container_name = "extractions"
    model = Extraction

    async def get_extraction(self, extraction_id: str, source_id: str) -> Optional[Extraction]:
        return await self.get(extraction_id, partition_value=source_id)

    async def history_for_source(self, source_id: str) -> list[Extraction]:
        return await self._query(
            "SELECT * FROM c ORDER BY c.created_at DESC", [], partition_value=source_id
        )

    async def active_for_source(self, source_id: str) -> Optional[Extraction]:
        rows = await self._query(
            "SELECT * FROM c WHERE c.status = @active",
            [{"name": "@active", "value": ExtractionStatus.ACTIVE.value}],
            partition_value=source_id,
        )
        return rows[0] if rows else None
