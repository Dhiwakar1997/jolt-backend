"""Supersede reconciliation: apply a re-read's concepts to the old ones (BE-07 §6, LLD §7.7).

When a source is read again, the agent passes the concepts it found to
`jolt_store_extraction` (`concepts`). They are classified against the concepts
the replaced read produced (`jolt.domain.reconciliation`, per track) and each
class is applied:

    unchanged  existing concept kept; credited to the new read
    refined    existing concept's text (title, key, page) updated in place;
               questions, reviews and card kept
    changed    existing concept superseded by a successor: a new concept whose
               card inherits the old card's FSRS state (`ConceptState.carried`);
               the old concept's questions are retired, it leaves the queue,
               and its reviews stay on it as history. The agent writes
               questions for the successor.
    removed    existing concept superseded with no successor: out of the queue,
               kept for history
    new        a new concept with a card due now (the agent writes questions)

**Ceiling and checkpoint.** Cosmos has no multi-document transaction across
partitions (concepts by track, cards by user, questions by concept), so a
supersede is made safe by ordering and idempotency, not atomicity (LLD §8). The
whole plan is classified up front and stored on the new read with a cursor; at
most `RECONCILE_MAX_CONCEPTS` (25) steps are applied per call and the cursor is
saved after each step. A larger supersede returns `status: "partial"` and the
agent calls `jolt_continue_reconciliation` until it is `complete`. Each step's
resulting ids are fixed in the plan (deterministic ids for new and successor
concepts and cards), and every write in a step converges when repeated, so a
crash mid-step is repaired by simply resuming.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from jolt.domain.models import (
    CarriedCard,
    Concept,
    ConceptState,
    ConceptStatus,
    Extraction,
    ExtractionConcept,
    ProposedConceptDoc,
    QuestionStatus,
    ReconcileStep,
    ReconciliationCheckpoint,
    Source,
    SyllabusItem,
)
from jolt.domain.reconciliation import ChangeClass, ProposedConcept, reconcile
from jolt.runtime import Runtime
from jolt.services.ownership import NotOwned, owned_track
from jolt.services.tracks import TrackError, TrackService

# Proposed concepts accepted in one store call (the plan itself is one document).
MAX_PROPOSED = 500


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(*parts: str) -> str:
    return hashlib.sha256(":".join(parts).encode()).hexdigest()[:32]


def step_concept_id(extraction_id: str, index: int) -> str:
    """The id of the concept a new / changed step creates (fixed in the plan)."""
    return _hash("reconcile", extraction_id, str(index))


def card_id(concept_id: str) -> str:
    """The id of the card created with a reconciled concept."""
    return _hash("card", concept_id)


class ReconcileError(Exception):
    """A recoverable problem the agent can fix (`{error}`)."""


@dataclass
class DeltaView:
    index: Optional[int]
    change: str
    concept_id: Optional[str]
    existing_concept_id: Optional[str]
    similarity: float
    applied: bool


@dataclass
class ReconcileView:
    status: str  # complete | partial
    total_steps: int
    applied_steps: int
    max_steps_per_call: int
    concept_ids: list[Optional[str]] = field(default_factory=list)  # input order
    deltas: list[DeltaView] = field(default_factory=list)

    @property
    def remaining_steps(self) -> int:
        return self.total_steps - self.applied_steps


def _title(text: str, title: Optional[str]) -> str:
    return (title or text).strip()[:120]


class ReconcileService:
    def __init__(self, rt: Runtime) -> None:
        self._rt = rt

    @property
    def _ceiling(self) -> int:
        return self._rt.settings.reconcile_max_concepts

    # -- plan -----------------------------------------------------------------
    async def prepare(
        self, user_id: str, source: Source, concepts: list[dict]
    ) -> list[ProposedConceptDoc]:
        """Validate a read's `concepts` before anything is written.

        Raises `ReconcileError` (recoverable: fix the input) or `NotOwned` (a
        track that is not the caller's own).
        """
        if len(concepts) > MAX_PROPOSED:
            raise ReconcileError(f"at most {MAX_PROPOSED} concepts per read")
        proposed: list[ProposedConceptDoc] = []
        for c in concepts:
            if not isinstance(c, dict):
                raise ReconcileError("each concept must be an object")
            text = (c.get("text") or "").strip()
            if not text:
                raise ReconcileError("each concept needs a non-empty text")
            track_id = c.get("track_id") or source.track_id
            if not track_id:
                raise ReconcileError("each concept needs a track_id (the source has no track)")
            page = c.get("page")
            proposed.append(
                ProposedConceptDoc(
                    text=text,
                    track_id=track_id,
                    title=((c.get("title") or "").strip()[:120] or None),
                    concept_key=c.get("concept_key") or c.get("syllabus_ref"),
                    parent=c.get("parent"),
                    page=page if isinstance(page, int) and page >= 1 else None,
                )
            )
        for track_id in {p.track_id for p in proposed}:
            await owned_track(self._rt, user_id, track_id)
        return proposed

    async def start(
        self,
        user_id: str,
        source: Source,
        extraction: Extraction,
        proposed: list[ProposedConceptDoc],
    ) -> ReconcileView:
        """Classify `proposed` (from `prepare`) against the replaced read's
        concepts, store the plan on `extraction`, and apply the first batch."""
        history = await self._rt.repos.extractions.history_for_source(source.id)
        previous = next((e for e in history if e.superseded_by == extraction.id), None)
        existing = await self._existing(user_id, previous)

        steps: list[ReconcileStep] = []
        removed: list[ReconcileStep] = []
        tracks = {p.track_id for p in proposed} | {c.track_id for c in existing}
        by_index: dict[int, ReconcileStep] = {}
        for track_id in sorted(tracks):
            indexes = [i for i, p in enumerate(proposed) if p.track_id == track_id]
            olds = [c for c in existing if c.track_id == track_id]
            result = reconcile(
                olds,
                [
                    ProposedConcept(
                        text=proposed[i].text,
                        title=proposed[i].title,
                        syllabus_ref=proposed[i].concept_key,
                    )
                    for i in indexes
                ],
            )
            n = 0
            for delta in result.deltas:
                if delta.change == ChangeClass.REMOVED:
                    removed.append(
                        ReconcileStep(
                            change=delta.change.value,
                            existing_concept_id=delta.existing.id,
                            existing_track_id=delta.existing.track_id,
                        )
                    )
                    continue
                index = indexes[n]
                n += 1
                keeps = delta.change in (ChangeClass.UNCHANGED, ChangeClass.REFINED)
                by_index[index] = ReconcileStep(
                    change=delta.change.value,
                    index=index,
                    existing_concept_id=delta.existing.id if delta.existing else None,
                    existing_track_id=delta.existing.track_id if delta.existing else None,
                    concept_id=(
                        delta.existing.id if keeps else step_concept_id(extraction.id, index)
                    ),
                    proposed=proposed[index],
                    similarity=round(delta.similarity, 3),
                )
        steps = [by_index[i] for i in range(len(proposed))] + removed

        checkpoint = ReconciliationCheckpoint(
            previous_extraction_id=previous.id if previous else None,
            steps=steps,
            status="complete" if not steps else "in_progress",
        )
        for _ in range(5):
            current = await self._rt.repos.extractions.get_extraction(extraction.id, source.id)
            if current is None:
                raise ReconcileError("the read was not stored")
            current.reconciliation = checkpoint
            if await self._rt.repos.extractions.replace(current, etag=current.etag) is not None:
                break
        return await self.resume(user_id, source.id)

    async def _existing(self, user_id: str, previous: Optional[Extraction]) -> list[Concept]:
        """The replaced read's concepts that are still the caller's and active."""
        if previous is None or not previous.concepts:
            return []
        ids = {c.concept_id for c in previous.concepts}
        cards = await self._rt.repos.concept_states.all_for_user(user_id)
        tracks = {
            c.track_id for c in cards if c.concept_id in ids and c.track_id and not c.superseded_at
        }
        out: list[Concept] = []
        for track_id in sorted(tracks):
            for concept in await self._rt.repos.concepts.list_for_track(track_id):
                if concept.id in ids:
                    out.append(concept)
        out.sort(key=lambda c: (c.created_at, c.id))
        return out

    # -- apply ----------------------------------------------------------------
    async def resume(self, user_id: str, source_id: str) -> ReconcileView:
        """Apply up to the ceiling of the source's pending steps; report progress."""
        source = await self._rt.repos.sources.get_source(source_id, user_id)
        if source is None:
            raise NotOwned("source not found")
        extraction = (
            await self._rt.repos.extractions.get_extraction(source.active_extraction_id, source.id)
            if source.active_extraction_id
            else None
        )
        if extraction is None or extraction.reconciliation is None:
            raise ReconcileError(
                "no reconciliation for this source: store the read with `concepts`"
            )
        budget = self._ceiling
        applied: list[ReconcileStep] = []
        while budget > 0:
            cp = extraction.reconciliation
            if cp.cursor >= len(cp.steps):
                break
            step = cp.steps[cp.cursor]
            credit = await self._apply(user_id, source, extraction.id, step)
            extraction = await self._advance(source.id, extraction.id, cp.cursor, credit)
            applied.append(step)
            budget -= 1
        await self._grow_agendas(user_id, applied)
        return self._view(extraction.reconciliation)

    async def _advance(
        self, source_id: str, extraction_id: str, cursor: int, credit: Optional[ExtractionConcept]
    ) -> Extraction:
        """Save the step at `cursor` as done (and its credit on the read)."""
        for _ in range(8):
            e = await self._rt.repos.extractions.get_extraction(extraction_id, source_id)
            assert e is not None and e.reconciliation is not None
            cp = e.reconciliation
            if cp.cursor != cursor:
                return e  # a concurrent resume already advanced past this step
            if credit is not None and all(c.concept_id != credit.concept_id for c in e.concepts):
                e.concepts.append(credit)
            cp.cursor = cursor + 1
            cp.updated_at = _now()
            if cp.cursor >= len(cp.steps):
                cp.status = "complete"
            saved = await self._rt.repos.extractions.replace(e, etag=e.etag)
            if saved is not None:
                return saved
        raise ReconcileError("the read kept changing; call jolt_continue_reconciliation")

    async def _apply(
        self, user_id: str, source: Source, extraction_id: str, step: ReconcileStep
    ) -> Optional[ExtractionConcept]:
        change = ChangeClass(step.change)
        p = step.proposed
        if change in (ChangeClass.UNCHANGED, ChangeClass.REFINED):
            assert p is not None and step.existing_concept_id and step.existing_track_id
            refined = change == ChangeClass.REFINED

            def edit(c: Concept) -> bool:
                before = c.model_dump()
                c.extraction_id = extraction_id
                if refined:
                    c.text = p.text
                    c.title = p.title or c.title
                    c.syllabus_ref = p.concept_key or c.syllabus_ref
                    c.page = p.page or c.page
                return c.model_dump() != before

            concept = await self._edit_concept(
                step.existing_concept_id, step.existing_track_id, edit
            )
            if concept is None:
                return None
            return ExtractionConcept(
                concept_id=concept.id, title=_title(concept.text, concept.title), is_new=False
            )

        if change == ChangeClass.REMOVED:
            assert step.existing_concept_id and step.existing_track_id
            await self._supersede(user_id, step.existing_concept_id, step.existing_track_id, None)
            return None

        assert p is not None and step.concept_id
        old: Optional[Concept] = None
        old_card: Optional[ConceptState] = None
        if change == ChangeClass.CHANGED:
            assert step.existing_concept_id and step.existing_track_id
            old = await self._rt.repos.concepts.get_concept(
                step.existing_concept_id, step.existing_track_id
            )
            old_card = await self._rt.repos.concept_states.for_concept(
                user_id, step.existing_concept_id
            )
        concept = Concept(
            id=step.concept_id,
            track_id=p.track_id,
            source_id=source.id,
            extraction_id=extraction_id,
            text=p.text,
            title=p.title,
            syllabus_ref=p.concept_key or (old.syllabus_ref if old else None),
            page=p.page,
            status=ConceptStatus.ACTIVE,
        )
        concept, _ = await self._rt.repos.concepts.create_if_absent(
            concept, partition_value=p.track_id
        )
        card = ConceptState(
            id=card_id(concept.id),
            user_id=user_id,
            concept_id=concept.id,
            track_id=p.track_id,
            due_at=_now(),
            active_question_ids=[],
        )
        if old_card is not None:
            # Carry the schedule forward: same card state, now on the successor.
            carried = CarriedCard(
                stability=old_card.stability,
                difficulty=old_card.difficulty,
                due_at=old_card.due_at,
                last_review_at=old_card.last_review_at,
                reps=old_card.reps,
                lapses=old_card.lapses,
                fsrs_state=old_card.fsrs_state,
                step=old_card.step,
            )
            card = card.model_copy(
                update={
                    **carried.model_dump(),
                    "carried": carried,
                    "carried_from": old_card.concept_id,
                }
            )
        await self._rt.repos.concept_states.create_if_absent(card, partition_value=user_id)
        if change == ChangeClass.CHANGED:
            # Stamp the successor first, then move the old concept out of the way.
            assert step.existing_concept_id and step.existing_track_id
            await self._retire_questions(step.existing_concept_id)
            await self._supersede(
                user_id, step.existing_concept_id, step.existing_track_id, concept.id
            )
        return ExtractionConcept(
            concept_id=concept.id,
            title=_title(concept.text, concept.title),
            is_new=change == ChangeClass.NEW,
        )

    async def _edit_concept(self, concept_id: str, track_id: str, edit) -> Optional[Concept]:
        for _ in range(5):
            concept = await self._rt.repos.concepts.get_concept(concept_id, track_id)
            if concept is None:
                return None
            if not edit(concept):
                return concept
            saved = await self._rt.repos.concepts.replace(concept, etag=concept.etag)
            if saved is not None:
                return saved
        return None

    async def _retire_questions(self, concept_id: str) -> None:
        now = _now()
        for _ in range(3):  # a lost etag race leaves the question active: go again
            active = await self._rt.repos.questions.list_for_concept(concept_id)
            if not active:
                return
            for q in active:
                q.status = QuestionStatus.RETIRED
                q.retired_at = now
                await self._rt.repos.questions.replace(q, etag=q.etag)

    async def _supersede(
        self, user_id: str, concept_id: str, track_id: str, successor_id: Optional[str]
    ) -> None:
        now = _now()

        def edit(c: Concept) -> bool:
            if c.status == ConceptStatus.SUPERSEDED and c.successor_id == successor_id:
                return False
            c.status = ConceptStatus.SUPERSEDED
            c.successor_id = successor_id
            c.superseded_at = c.superseded_at or now
            return True

        await self._edit_concept(concept_id, track_id, edit)
        for _ in range(5):
            state = await self._rt.repos.concept_states.for_concept(user_id, concept_id)
            if state is None or (state.superseded_at is not None and state.due_at is None):
                return
            state.superseded_at = state.superseded_at or now
            state.due_at = None
            if await self._rt.repos.concept_states.replace(state, etag=state.etag) is not None:
                return

    async def _grow_agendas(self, user_id: str, steps: list[ReconcileStep]) -> None:
        """Auto-add the keys of new, changed and refined concepts (as jolt_log_session does)."""
        by_track: dict[str, list[SyllabusItem]] = {}
        for step in steps:
            p = step.proposed
            if p is None or not p.concept_key or step.change == ChangeClass.UNCHANGED.value:
                continue
            by_track.setdefault(p.track_id, []).append(
                SyllabusItem(
                    concept_key=p.concept_key, label=p.title or p.concept_key, parent=p.parent
                )
            )
        service = TrackService(self._rt)
        for track_id, items in by_track.items():
            try:
                await service.auto_add_concepts(user_id, track_id, items)
            except TrackError:
                continue

    def _view(self, cp: ReconciliationCheckpoint) -> ReconcileView:
        indexed = sorted((s for s in cp.steps if s.index is not None), key=lambda s: s.index)
        return ReconcileView(
            status="complete" if cp.cursor >= len(cp.steps) else "partial",
            total_steps=len(cp.steps),
            applied_steps=min(cp.cursor, len(cp.steps)),
            max_steps_per_call=self._ceiling,
            concept_ids=[s.concept_id for s in indexed],
            deltas=[
                DeltaView(
                    index=s.index,
                    change=s.change,
                    concept_id=s.concept_id,
                    existing_concept_id=s.existing_concept_id,
                    similarity=s.similarity,
                    applied=i < cp.cursor,
                )
                for i, s in enumerate(cp.steps)
            ],
        )
