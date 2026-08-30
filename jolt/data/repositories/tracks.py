"""Tracks repository (LLD §5 curated-track exception, §6).

User tracks are partitioned by /user_id. Curated tracks (`origin='jolt'`) have
user_id=None and are world-readable; they live in the same container under a
sentinel partition bucket so the global read is a single-partition point/query,
never a cross-partition fan-out over user data.
"""

from __future__ import annotations

from typing import Optional

from jolt.data.repositories.base import Repository
from jolt.domain.models import Track, TrackOrigin

# Sentinel partition value for curated (user_id=None) tracks.
CURATED_BUCKET = "__jolt_curated__"


class TracksRepository(Repository[Track]):
    container_name = "tracks"
    model = Track

    def _partition_of(self, track: Track) -> str:
        return track.user_id or CURATED_BUCKET

    def _to_doc(self, model: Track) -> dict:
        doc = super()._to_doc(model)
        # Curated tracks must land in the curated bucket, not a null partition.
        if model.user_id is None:
            doc["user_id"] = CURATED_BUCKET
        return doc

    def _from_doc(self, doc: dict) -> Track:
        if doc.get("user_id") == CURATED_BUCKET:
            doc = {**doc, "user_id": None}
        return super()._from_doc(doc)

    async def create_track(self, track: Track) -> Track:
        return await self.create(track)

    async def list_for_user(self, user_id: str) -> list[Track]:
        return await self._query(
            "SELECT * FROM c ORDER BY c.created_at DESC", [], partition_value=user_id
        )

    async def list_curated(self) -> list[Track]:
        """World-readable curated tracks — single-partition on the curated bucket."""
        return await self._query(
            "SELECT * FROM c WHERE c.origin = @origin",
            [{"name": "@origin", "value": TrackOrigin.JOLT.value}],
            partition_value=CURATED_BUCKET,
        )

    async def get_track(self, track_id: str, user_id: Optional[str]) -> Optional[Track]:
        return await self.get(track_id, partition_value=user_id or CURATED_BUCKET)
