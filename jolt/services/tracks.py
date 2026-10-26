"""Tracks and coverage services (LLD §6, §7.5 progress views).

The track document holds the agenda (the shaped syllabus). Two write paths mutate
it in the same sync — auto-add (per-concept growth, from jolt_log_session) and
refine (jolt_set_agenda) — so every agenda write is an etag-guarded
read-modify-write with retry: a refine must never clobber a concurrent auto-add,
and vice versa. The backend only stores agendas; it never generates or infers one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional

from jolt.domain.agenda import outline_to_syllabus
from jolt.domain.coverage import CoverageReport, compute_coverage
from jolt.domain.models import (
    Agenda,
    AgendaSource,
    AgendaStatus,
    SyllabusItem,
    Track,
    TrackKind,
    TrackOrigin,
)
from jolt.runtime import Runtime
from jolt.services.ownership import NotOwned, visible_concepts, visible_track

# How many times to re-read and retry an agenda write that lost the etag race.
_AGENDA_WRITE_RETRIES = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TrackError(Exception):
    pass


class AgendaLocked(TrackError):
    """A write was attempted against a locked agenda (refinement is frozen)."""


class TrackService:
    def __init__(self, rt: Runtime) -> None:
        self._rt = rt

    async def list_tracks(self, user_id: str) -> list[Track]:
        """A user's own tracks plus the world-readable curated ones (LLD §5)."""
        own = await self._rt.repos.tracks.list_for_user(user_id)
        curated = await self._rt.repos.tracks.list_curated()
        return own + curated

    async def create_track(
        self,
        user_id: str,
        name: str,
        syllabus: list[str],
        *,
        kind: TrackKind = TrackKind.NOTES,
        outline: Optional[str] = None,
    ) -> Track:
        """Create a learner's track (BE-07 §1).

        * A syllabus (labels) is a deliberate, user-authored agenda: `refined`.
        * An outline gives a `draft` agenda (source `user`) for the model to tidy
          at the next sync: the outline is kept verbatim and cut mechanically
          into draft items (`jolt.domain.agenda`).
        * Neither leaves the agenda `none`; it grows from uploads (auto-add).
        """
        outline = (outline or "").strip() or None
        if outline is not None:
            items = outline_to_syllabus(outline)
            agenda = Agenda(
                status=AgendaStatus.DRAFT,
                source=AgendaSource.USER,
                syllabus=items,
                outline=outline,
            )
        else:
            items = [
                SyllabusItem(concept_key=label, label=label, source=AgendaSource.USER)
                for label in syllabus
            ]
            agenda = Agenda(
                status=AgendaStatus.REFINED if items else AgendaStatus.NONE,
                source=AgendaSource.USER,
                syllabus=items,
                last_refined_at=_now() if items else None,
            )
        track = Track(
            user_id=user_id,
            name=name,
            origin=TrackOrigin.USER,
            agenda=agenda,
            kind=kind,
        )
        return await self._rt.repos.tracks.create_track(track)

    async def update_track(
        self,
        user_id: str,
        track_id: str,
        *,
        name: Optional[str] = None,
        archived: Optional[bool] = None,
    ) -> Track:
        """Rename and/or archive (unarchive) one of the caller's tracks."""

        def mutate(track: Track) -> bool:
            changed = False
            if name is not None and name != track.name:
                track.name = name
                changed = True
            if archived is not None and archived != (track.archived_at is not None):
                track.archived_at = _now() if archived else None
                changed = True
            return changed

        return await self._mutate_agenda(user_id, track_id, mutate, owned_only=True)

    # -- agenda reads -------------------------------------------------------
    async def _get_track_any(self, user_id: str, track_id: str) -> Track:
        """Load a track from the user's partition, falling back to curated."""
        track = await self._rt.repos.tracks.get_track(track_id, user_id)
        if track is None:
            track = await self._rt.repos.tracks.get_track(track_id, None)
        if track is None:
            raise TrackError("track not found")
        return track

    async def get_agenda(self, user_id: str, track_id: str) -> Agenda:
        track = await self._get_track_any(user_id, track_id)
        return track.agenda

    # -- agenda writes (etag-guarded read-modify-write) ---------------------
    async def _mutate_agenda(
        self,
        user_id: str,
        track_id: str,
        mutate: Callable[[Track], bool],
        *,
        owned_only: bool = False,
    ) -> Track:
        """Serialise an agenda mutation behind optimistic concurrency.

        `mutate(track)` edits the agenda in place and returns True to persist or
        False to abort as a no-op. On an etag mismatch (a concurrent auto-add or
        refine committed first) we re-read and re-apply, so no entries are lost.
        `mutate` may raise AgendaLocked; that is re-raised without retry.
        """
        for _ in range(_AGENDA_WRITE_RETRIES):
            if owned_only:
                track = await self._rt.repos.tracks.get_track(track_id, user_id)
                if track is None:
                    raise TrackError("track not found")
            else:
                track = await self._get_track_any(user_id, track_id)
            if not mutate(track):
                return track
            updated = await self._rt.repos.tracks.replace(track, etag=track.etag)
            if updated is not None:
                return updated
        raise TrackError("agenda write conflict; retries exhausted")

    async def set_agenda(
        self,
        user_id: str,
        track_id: str,
        syllabus: list[SyllabusItem],
        source: AgendaSource,
        *,
        mark_refined: bool = False,
        refined_from_source_id: Optional[str] = None,
    ) -> Agenda:
        """Replace the agenda's syllabus. Rejected if the agenda is locked.

        Replace-semantics: the supplied syllabus wholly replaces the stored one, so
        re-running a refine with the same input is idempotent (no double-apply).
        """

        def mutate(track: Track) -> bool:
            if track.agenda.status == AgendaStatus.LOCKED:
                raise AgendaLocked("agenda is locked; refinement is frozen")
            # Per-item provenance: an item that keeps an existing key keeps its
            # origin (an auto-added item stays "from your notes"); a new item is
            # credited to this write's source, unless it names its own.
            before = {i.concept_key: track.agenda.item_source(i) for i in track.agenda.syllabus}
            track.agenda.syllabus = [
                item.model_copy(
                    update={"source": item.source or before.get(item.concept_key) or source}
                )
                for item in syllabus
            ]
            track.agenda.source = source
            if mark_refined:
                track.agenda.status = AgendaStatus.REFINED
                track.agenda.last_refined_at = _now()
            elif track.agenda.status == AgendaStatus.NONE:
                track.agenda.status = AgendaStatus.DRAFT
            if refined_from_source_id is not None:
                track.agenda.refined_from_source_id = refined_from_source_id
            return True

        # Curated tracks are read-only: only the caller's own agenda can be written.
        track = await self._mutate_agenda(user_id, track_id, mutate, owned_only=True)
        return track.agenda

    async def auto_add_concepts(
        self,
        user_id: str,
        track_id: str,
        items: list[SyllabusItem],
    ) -> Agenda:
        """Per-concept growth path: add any concept_key not already in the agenda.

        Keyed on concept_key membership, so it is naturally idempotent — re-running
        a sync adds nothing new. Skipped entirely when the agenda is locked.
        """

        def mutate(track: Track) -> bool:
            if track.agenda.status == AgendaStatus.LOCKED:
                return False  # locked: auto-add is silently skipped, not an error
            existing = track.agenda.keys()
            added = False
            for item in items:
                if item.concept_key in existing:
                    continue
                # Only keep a parent that actually resolves to a known key.
                parent = item.parent if item.parent in existing else None
                track.agenda.syllabus.append(
                    SyllabusItem(
                        concept_key=item.concept_key,
                        label=item.label,
                        parent=parent,
                        source=AgendaSource.EMERGENT,
                    )
                )
                existing.add(item.concept_key)
                added = True
            if added and track.agenda.status == AgendaStatus.NONE:
                # The agenda grew to fit what was learned.
                track.agenda.status = AgendaStatus.DRAFT
                track.agenda.source = AgendaSource.EMERGENT
            return added

        track = await self._mutate_agenda(user_id, track_id, mutate, owned_only=True)
        return track.agenda

    async def set_lock(self, user_id: str, track_id: str, locked: bool) -> Agenda:
        """Lock/unlock the agenda (the app's toggle). Owned tracks only.

        Locking freezes refinement; unlocking resumes it, returning to 'refined'
        when a syllabus exists (else 'none').
        """

        def mutate(track: Track) -> bool:
            if locked:
                if track.agenda.status == AgendaStatus.LOCKED:
                    return False
                track.agenda.status = AgendaStatus.LOCKED
            else:
                if track.agenda.status != AgendaStatus.LOCKED:
                    return False
                track.agenda.status = (
                    AgendaStatus.REFINED if track.agenda.syllabus else AgendaStatus.NONE
                )
            return True

        track = await self._mutate_agenda(user_id, track_id, mutate, owned_only=True)
        return track.agenda

    async def coverage(self, user_id: str, track_id: str) -> CoverageReport:
        """Coverage of a visible track by the caller's own concepts.

        Two single-partition queries joined in memory (LLD §6): the track's
        concepts and the caller's cards on it. Concepts without the caller's
        card (other learners' rows on a curated track) are not counted.
        """
        try:
            track = await visible_track(self._rt, user_id, track_id)
            concepts, states = await visible_concepts(self._rt, user_id, track_id)
        except NotOwned as exc:
            raise TrackError(str(exc)) from exc
        return compute_coverage(track, concepts, list(states.values()))
