"""Sync + extraction services (LLD §7.2, §7.7).

These back the MCP tool surface. Everything the agent does (reading files, running
extraction, grading) runs on the agent's own subscription; Jolt only writes rows.

Grade submission recomputes FSRS **inline** (single-service course correction):
each final grade is applied and the affected card is re-folded in the same handler,
which is what lets a retroactive semantic grade correct an earlier provisional one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from jolt.data.leases import SourceLeases
from jolt.domain.grading import GradingInput, apply_grading
from jolt.domain.models import (
    Concept,
    ConceptState,
    ConceptStatus,
    Extraction,
    ExtractionStatus,
    ProcessingStatus,
    Question,
    Review,
    Session,
    Source,
    SyllabusItem,
)
from jolt.domain.reconciliation import ProposedConcept, reconcile
from jolt.domain.scheduling import fold_history
from jolt.runtime import Runtime
from jolt.services.tracks import TrackError, TrackService


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SyncError(Exception):
    pass


# --------------------------------------------------------------------------- #
# DTOs crossing the MCP boundary
# --------------------------------------------------------------------------- #
@dataclass
class SyncPlan:
    unprocessed_sources: int
    pending_gradings: int


@dataclass
class SourceContent:
    source_id: str
    filename: str
    content_type: str
    read_url: str


@dataclass
class ConceptInput:
    text: str
    track_id: str
    title: Optional[str] = None
    syllabus_ref: Optional[str] = None
    source_id: Optional[str] = None
    # Agenda linkage. `concept_key` is the stable key coverage/auto-add join on;
    # it defaults to `syllabus_ref` for backward-compatible callers. `parent` is an
    # optional existing concept_key to nest under when auto-added.
    concept_key: Optional[str] = None
    parent: Optional[str] = None

    def effective_key(self) -> Optional[str]:
        return self.concept_key or self.syllabus_ref


@dataclass
class QuestionInput:
    stem: str
    options: list[str]
    correct_index: int
    expected_answer: str


@dataclass
class PendingGrading:
    review_id: str
    concept_id: str
    question_id: str
    free_text: str
    expected_answer: str


class SyncService:
    def __init__(self, rt: Runtime) -> None:
        self._rt = rt
        self._source_leases = SourceLeases(rt.repos.sources)

    # -- plan ---------------------------------------------------------------
    async def plan(self, user_id: str, limit: int = 50) -> SyncPlan:
        sources = await self._rt.repos.sources.list_unprocessed(user_id, limit)
        claimable = [s for s in sources if self._is_claimable(s)]
        pending = await self._rt.repos.reviews.list_pending_gradings(user_id, limit)
        return SyncPlan(unprocessed_sources=len(claimable), pending_gradings=len(pending))

    @staticmethod
    def _is_claimable(source: Source) -> bool:
        if source.processing_status == ProcessingStatus.UNPROCESSED:
            return True
        return (
            source.processing_status == ProcessingStatus.IN_FLIGHT
            and source.lease_expires_at is not None
            and source.lease_expires_at <= _now()
        )

    # -- source intake ------------------------------------------------------
    async def get_unprocessed_sources(self, user_id: str, limit: int = 10) -> list[Source]:
        """Claim up to `limit` sources under a lease (LLD §6). Losers are skipped."""
        candidates = await self._rt.repos.sources.list_unprocessed(user_id, limit)
        claimed: list[Source] = []
        for source in candidates:
            if not self._is_claimable(source):
                continue
            leased = await self._source_leases.claim(source)
            if leased is not None:  # None => lost the etag race to another sync
                claimed.append(leased)
        return claimed

    async def get_source_content(self, user_id: str, source_id: str) -> SourceContent:
        source = await self._rt.repos.sources.get_source(source_id, user_id)
        if source is None:
            raise SyncError("source not found")
        read_url = await self._rt.blob.read_url(source.blob_path)
        return SourceContent(
            source_id=source.id,
            filename=source.filename,
            content_type=source.content_type,
            read_url=read_url,
        )

    async def store_extraction(
        self,
        user_id: str,
        source_id: str,
        markdown: str,
        confidence: float,
        model_id: str | None = None,
        supersede: bool = False,
    ) -> Extraction:
        """Persist an extraction and move the source's active pointer (LLD §7.7).

        Stamp-before-move ordering: the new extraction is written, then (on
        supersede) the previous active one is marked superseded, then the source's
        pointer moves. A crash between steps leaves the old active extraction
        canonical and replayable.
        """
        source = await self._rt.repos.sources.get_source(source_id, user_id)
        if source is None:
            raise SyncError("source not found")

        extraction = Extraction(
            source_id=source_id,
            markdown=markdown,
            confidence=confidence,
            model_id=model_id,
            status=ExtractionStatus.ACTIVE,
        )
        extraction = await self._rt.repos.extractions.create(extraction)

        if supersede:
            previous = await self._rt.repos.extractions.active_for_source(source_id)
            if previous and previous.id != extraction.id:
                previous.status = ExtractionStatus.SUPERSEDED
                previous.superseded_by = extraction.id
                await self._rt.repos.extractions.replace(previous, etag=previous.etag)

        await self._source_leases.release_processed(source, extraction.id)
        return extraction

    # -- session / concepts / questions ------------------------------------
    async def log_session(self, user_id: str, source_ids: list[str], concepts: list[ConceptInput]):
        """Create a session, its concepts, and a fresh FSRS state per concept.

        Also grows each track's agenda: any concept whose `concept_key` is not yet
        in the track's agenda syllabus is auto-added (as a leaf, or under a supplied
        parent). Skipped on locked agendas. Auto-add is keyed on concept_key
        membership, so re-running a sync never duplicates entries.
        """
        created_concepts: list[Concept] = []
        for c in concepts:
            concept = Concept(
                track_id=c.track_id,
                source_id=c.source_id,
                text=c.text,
                # The concept's coverage key is its agenda concept_key.
                syllabus_ref=c.effective_key(),
                status=ConceptStatus.ACTIVE,
            )
            concept = await self._rt.repos.concepts.create(concept)
            created_concepts.append(concept)
            # New card: due now so the material enters the review rotation.
            state = ConceptState(
                user_id=user_id,
                concept_id=concept.id,
                track_id=c.track_id,
                due_at=_now(),
            )
            await self._rt.repos.concept_states.create(state)

        await self._auto_add_agenda(user_id, concepts)

        session = Session(
            user_id=user_id,
            source_ids=source_ids,
            concept_ids=[c.id for c in created_concepts],
        )
        session = await self._rt.repos.sessions.create(session)
        return session, created_concepts

    async def _auto_add_agenda(self, user_id: str, concepts: list[ConceptInput]) -> None:
        """Grow each involved track's agenda from the session's concepts.

        Grouped by track so each track's agenda is a single etag-guarded
        read-modify-write (serialised against a concurrent refine).
        """
        by_track: dict[str, list[SyllabusItem]] = {}
        for c in concepts:
            key = c.effective_key()
            if not key:
                continue  # no agenda linkage → nothing to add
            by_track.setdefault(c.track_id, []).append(
                SyllabusItem(concept_key=key, label=c.title or key, parent=c.parent)
            )
        if not by_track:
            return
        track_service = TrackService(self._rt)
        for track_id, items in by_track.items():
            try:
                await track_service.auto_add_concepts(user_id, track_id, items)
            except TrackError:
                # A missing track shouldn't fail the whole session log.
                continue

    async def create_questions(
        self, user_id: str, concept_id: str, questions: list[QuestionInput]
    ) -> list[Question]:
        created: list[Question] = []
        for q in questions:
            question = Question(
                concept_id=concept_id,
                stem=q.stem,
                options=q.options,
                correct_index=q.correct_index,
                expected_answer=q.expected_answer,
            )
            created.append(await self._rt.repos.questions.create(question))
        return created

    # -- gradings -----------------------------------------------------------
    async def get_pending_gradings(self, user_id: str, limit: int = 10) -> list[PendingGrading]:
        reviews = await self._rt.repos.reviews.list_pending_gradings(user_id, limit)
        out: list[PendingGrading] = []
        for review in reviews:
            question = await self._rt.repos.questions.get_question(
                review.question_id, review.concept_id
            )
            out.append(
                PendingGrading(
                    review_id=review.id,
                    concept_id=review.concept_id,
                    question_id=review.question_id,
                    free_text=review.free_text or "",
                    expected_answer=question.expected_answer if question else "",
                )
            )
        return out

    async def submit_gradings(self, user_id: str, gradings: list[GradingInput]) -> int:
        """Apply semantic grades idempotently and re-fold each affected card inline.

        Returns the number of gradings newly applied (idempotent no-ops excluded).
        """
        applied = 0
        touched_concepts: set[str] = set()
        for g in gradings:
            review = await self._rt.repos.reviews.get_review(g.review_id, user_id)
            if review is None:
                continue
            result = apply_grading(review, g)
            if not result.applied:
                continue
            await self._rt.repos.reviews.replace(result.review, etag=result.review.etag)
            if result.feedback is not None:
                await self._rt.repos.answer_feedback.create(result.feedback)
            applied += 1
            touched_concepts.add(review.concept_id)

        # Inline FSRS re-fold for each concept whose grade changed (LLD §7.4).
        for concept_id in touched_concepts:
            await self._recompute_card(user_id, concept_id)
        return applied

    async def _recompute_card(self, user_id: str, concept_id: str) -> ConceptState:
        history = await self._rt.repos.reviews.history_for_concept(user_id, concept_id)
        state = await self._rt.repos.concept_states.for_concept(user_id, concept_id)
        if state is None:
            state = ConceptState(user_id=user_id, concept_id=concept_id)
            state = await self._rt.repos.concept_states.create(state)
        folded = fold_history(state, history)
        folded.id = state.id
        folded.etag = state.etag
        folded.track_id = state.track_id
        return await self._rt.repos.concept_states.replace(folded, etag=state.etag) or folded
