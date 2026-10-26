"""Sync runs repository (BE-04). Partition key = /user_id.

Every query is single-partition: a user's runs live together, one per night, so
the containers stay tiny and need no composite index.
"""

from __future__ import annotations

from jolt.data.repositories.base import Repository
from jolt.domain.models import SyncRun, SyncRunStatus


class SyncRunsRepository(Repository[SyncRun]):
    container_name = "sync_runs"
    model = SyncRun

    async def list_recent(self, user_id: str, limit: int) -> list[SyncRun]:
        """The user's runs, newest first."""
        return await self._query(
            "SELECT * FROM c ORDER BY c.started_at DESC OFFSET 0 LIMIT @limit",
            [{"name": "@limit", "value": limit}],
            partition_value=user_id,
        )

    async def list_running(self, user_id: str) -> list[SyncRun]:
        """Runs not closed yet (any client), newest first."""
        return await self._query(
            "SELECT * FROM c WHERE c.status = @running ORDER BY c.started_at DESC",
            [{"name": "@running", "value": SyncRunStatus.RUNNING.value}],
            partition_value=user_id,
        )

    async def latest_for_token(self, user_id: str, token_id: str) -> SyncRun | None:
        """The newest run opened with one MCP token, if any."""
        rows = await self._query(
            "SELECT * FROM c WHERE c.token_id = @tid ORDER BY c.started_at DESC OFFSET 0 LIMIT 1",
            [{"name": "@tid", "value": token_id}],
            partition_value=user_id,
        )
        return rows[0] if rows else None
