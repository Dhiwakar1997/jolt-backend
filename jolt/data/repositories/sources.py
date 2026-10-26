"""Sources and Extractions repositories (LLD §6, §7).

Sources partition by /user_id; extractions by /source_id (history read together).
"""

from __future__ import annotations

from datetime import datetime
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

    async def list_confirmed(
        self,
        user_id: str,
        *,
        track_id: Optional[str] = None,
        status: Optional[ProcessingStatus] = None,
    ) -> list[Source]:
        """Confirmed sources (the upload finished), newest first, optionally
        filtered by track and status. Unconfirmed rows are abandoned or
        in-progress uploads and stay out of the app's views (BE-05)."""
        clauses = ["c.confirmed_at != @none"]
        params: list[dict] = [{"name": "@none", "value": None}]
        if track_id is not None:
            clauses.append("c.track_id = @track")
            params.append({"name": "@track", "value": track_id})
        if status is not None:
            clauses.append("c.processing_status = @status")
            params.append({"name": "@status", "value": status.value})
        query = f"SELECT * FROM c WHERE {' AND '.join(clauses)} ORDER BY c.created_at DESC"
        return await self._query(query, params, partition_value=user_id)

    async def list_unconfirmed_before(self, cutoff: datetime) -> list[Source]:
        """Uploads never confirmed and requested before `cutoff`, across users.
        Maintenance only (the abandoned-upload sweep, BE-10)."""
        iterator = self._c.query_items(
            query="SELECT * FROM c WHERE c.confirmed_at = @none AND c.created_at < @cutoff",
            parameters=[
                {"name": "@none", "value": None},
                {"name": "@cutoff", "value": cutoff.isoformat()},
            ],
        )
        return [self._from_doc(doc) async for doc in iterator]

    async def list_unprocessed(self, user_id: str, limit: int) -> list[Source]:
        """Claimable sources: unprocessed, or in_flight with an expired lease.

        Only confirmed uploads (BE-05): a source whose bytes never arrived is not
        work for a sync. `now` is compared in the app rather than the query so
        the same predicate is reused by the lease layer.
        """
        query = (
            "SELECT * FROM c WHERE c.processing_status IN (@unproc, @inflight) "
            "AND c.confirmed_at != @none "
            "ORDER BY c.created_at ASC OFFSET 0 LIMIT @limit"
        )
        params = [
            {"name": "@unproc", "value": ProcessingStatus.UNPROCESSED.value},
            {"name": "@inflight", "value": ProcessingStatus.IN_FLIGHT.value},
            {"name": "@none", "value": None},
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
