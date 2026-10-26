"""Concept corrections (BE-07 §4).

"Something's wrong with this concept": the learner leaves a note in the app; the
note waits on their card (user partition) until the next sync, which reads it
with `jolt_get_pending_corrections` and fixes the concept with
`jolt_correct_concept`. Applying a correction (or re-confirming the text)
clears the card's pending notes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from jolt.domain.models import Concept, ConceptState, CorrectionNote
from jolt.runtime import Runtime
from jolt.services.ownership import owned_card, owned_concept
from jolt.services.reviews import ReviewService

# Notes kept per concept while waiting for a sync; older ones are dropped.
MAX_PENDING_NOTES = 20


def _now() -> datetime:
    return datetime.now(timezone.utc)


class CorrectionConflict(Exception):
    pass


@dataclass
class PendingCorrection:
    concept_id: str
    track_id: str
    title: str
    text: str
    notes: list[CorrectionNote] = field(default_factory=list)


@dataclass
class CorrectionResult:
    concept_id: str
    updated: bool
    corrections_cleared: int
    # True when the text was replaced (not a re-confirmation of the same text).
    text_changed: bool = False
    concept: Optional[Concept] = None


class CorrectionService:
    def __init__(self, rt: Runtime) -> None:
        self._rt = rt

    async def request(self, user_id: str, concept_id: str, note: str) -> CorrectionNote:
        """Queue a note on the caller's card for the next sync."""
        entry = CorrectionNote(note=note.strip())
        for _ in range(5):
            state = await owned_card(self._rt, user_id, concept_id)
            state.corrections = [*state.corrections, entry][-MAX_PENDING_NOTES:]
            state.correction_requested_at = state.correction_requested_at or entry.created_at
            if await self._rt.repos.concept_states.replace(state, etag=state.etag) is not None:
                return entry
        raise CorrectionConflict("the concept changed while saving the note; try again")

    async def pending(self, user_id: str) -> list[PendingCorrection]:
        """Every concept with notes waiting, oldest request first."""
        states = await self._rt.repos.concept_states.with_corrections(user_id)
        states.sort(key=lambda s: s.correction_requested_at or s.created_at)
        out: list[PendingCorrection] = []
        for state in states:
            if not state.corrections or not state.track_id:
                continue
            concept: Optional[Concept] = await self._rt.repos.concepts.get_concept(
                state.concept_id, state.track_id
            )
            if concept is None:
                continue
            out.append(
                PendingCorrection(
                    concept_id=concept.id,
                    track_id=concept.track_id,
                    title=ReviewService.concept_title(concept, concept.id),
                    text=concept.text,
                    notes=list(state.corrections),
                )
            )
        return out

    async def correct(
        self, user_id: str, track_id: str, concept_id: str, text: str
    ) -> CorrectionResult:
        """Replace the caller's concept text in place, then clear its pending notes.

        Raises `NotOwned` when the concept is not the caller's (or not on
        `track_id`). `updated` is False when the concept write lost a race.
        """
        concept, _state = await owned_concept(self._rt, user_id, concept_id, track_id=track_id)
        changed = concept.text != text
        if changed:
            concept.text = text
            saved = await self._rt.repos.concepts.replace(concept, etag=concept.etag)
            if saved is None:
                return CorrectionResult(concept_id, updated=False, corrections_cleared=0)
            concept = saved
        return CorrectionResult(
            concept_id,
            updated=True,
            corrections_cleared=await self._clear(user_id, concept_id),
            text_changed=changed,
            concept=concept,
        )

    async def _clear(self, user_id: str, concept_id: str) -> int:
        for _ in range(5):
            state: Optional[ConceptState] = await self._rt.repos.concept_states.for_concept(
                user_id, concept_id
            )
            if state is None or not state.corrections:
                return 0
            cleared = len(state.corrections)
            state.corrections = []
            state.correction_requested_at = None
            if await self._rt.repos.concept_states.replace(state, etag=state.etag) is not None:
                return cleared
        return 0
