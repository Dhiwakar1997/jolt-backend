"""Tracks and coverage services (LLD §6, §7.5 progress views)."""

from __future__ import annotations

from jolt.domain.coverage import CoverageReport, compute_coverage
from jolt.domain.models import Track, TrackOrigin
from jolt.runtime import Runtime


class TrackError(Exception):
    pass


class TrackService:
    def __init__(self, rt: Runtime) -> None:
        self._rt = rt

    async def list_tracks(self, user_id: str) -> list[Track]:
        """A user's own tracks plus the world-readable curated ones (LLD §5)."""
        own = await self._rt.repos.tracks.list_for_user(user_id)
        curated = await self._rt.repos.tracks.list_curated()
        return own + curated

    async def create_track(self, user_id: str, name: str, syllabus: list[str]) -> Track:
        track = Track(
            user_id=user_id,
            name=name,
            origin=TrackOrigin.USER,
            syllabus=syllabus,
        )
        return await self._rt.repos.tracks.create_track(track)

    async def coverage(self, user_id: str, track_id: str) -> CoverageReport:
        # Curated tracks read globally; user tracks read within the user partition.
        track = await self._rt.repos.tracks.get_track(track_id, user_id)
        if track is None:
            track = await self._rt.repos.tracks.get_track(track_id, None)
        if track is None:
            raise TrackError("track not found")

        concepts = await self._rt.repos.concepts.list_for_track(track_id)
        states = await self._rt.repos.concept_states.all_for_user(user_id)
        # Join in memory — no cross-partition query (LLD §6 query discipline).
        return compute_coverage(track, concepts, states)
