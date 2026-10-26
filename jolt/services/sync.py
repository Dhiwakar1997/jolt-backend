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
from jolt.domain.grading import GradingInput
from jolt.domain.models import (
    Concept,
    ConceptState,
    ConceptStatus,
    Extraction,
    ExtractionConcept,
    ExtractionStatus,
    ProcessingStatus,
    Question,
    Session,
    Source,
    SyllabusItem,
)
from jolt.runtime import Runtime
from jolt.services.gradings import GradingService
from jolt.services.reviews import ReviewService
from jolt.services.tracks import TrackError, TrackService


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SyncError(Exception):
    pass


# --------------------------------------------------------------------------- #
# DTOs crossing the MCP boundary
# --------------------------------------------------------------------------- #
@dataclass
class RereadItem:
    source_id: str
    title: str
    preferred_model: Optional[str]
    requested_at: datetime


@dataclass
class SyncPlan:
    unprocessed_sources: int
    pending_gradings: int
    # Sources the user asked to read again (BE-05 §4); also counted above.
    rereads: list[RereadItem] = field(default_factory=list)


@dataclass
class PageContent:
    index: int
    filename: str
    content_type: str
    read_url: str


@dataclass
class SourceContent:
    source_id: str
    filename: str
    content_type: str
    read_url: str
    pages: list[PageContent] = field(default_factory=list)


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
    # BE-06: the question as answered, so the agent can rewrite it (D8).
    stem: str = ""
    options: list[str] = field(default_factory=list)
    correct_index: int = 0
    question_version: int = 1
    selected_index: Optional[int] = None
    is_correct: Optional[bool] = None


class SyncService:
    def __init__(self, rt: Runtime) -> None:
        self._rt = rt
        self._source_leases = SourceLeases(rt.repos.sources)

    # -- plan ---------------------------------------------------------------
    async def plan(self, user_id: str, limit: int = 50) -> SyncPlan:
        sources = await self._rt.repos.sources.list_unprocessed(user_id, limit)
        claimable = [s for s in sources if self._is_claimable(s) and not self._exhausted(s)]
        pending = await self._rt.repos.reviews.list_pending_gradings(user_id, limit)
        rereads = [
            RereadItem(
                source_id=s.id,
                title=s.title or s.filename,
                preferred_model=s.reread_preferred_model,
                requested_at=s.reread_requested_at,
            )
            for s in claimable
            if s.reread_requested_at is not None
        ]
        return SyncPlan(
            unprocessed_sources=len(claimable), pending_gradings=len(pending), rereads=rereads
        )

    @staticmethod
    def _is_claimable(source: Source) -> bool:
        if source.processing_status == ProcessingStatus.UNPROCESSED:
            return True
        return (
            source.processing_status == ProcessingStatus.IN_FLIGHT
            and source.lease_expires_at is not None
            and source.lease_expires_at <= _now()
        )

    def _exhausted(self, source: Source) -> bool:
        """A lapsed lease on a source already claimed the maximum number of times."""
        return (
            source.processing_status == ProcessingStatus.IN_FLIGHT
            and source.claim_count >= self._rt.settings.source_max_attempts
        )

    async def _mark_failed(self, source: Source) -> None:
        source.processing_status = ProcessingStatus.FAILED
        source.lease_expires_at = None
        source.failed_reason = (
            f"not read: the sync lease lapsed {source.claim_count} times without an extraction"
        )
        await self._rt.repos.sources.replace(source, etag=source.etag)

    # -- source intake ------------------------------------------------------
    async def get_unprocessed_sources(self, user_id: str, limit: int = 10) -> list[Source]:
        """Claim up to `limit` sources under a lease (LLD §6). Losers are skipped."""
        candidates = await self._rt.repos.sources.list_unprocessed(user_id, limit)
        claimed: list[Source] = []
        for source in candidates:
            if not self._is_claimable(source):
                continue
            if self._exhausted(source):
                await self._mark_failed(source)  # BE-05: `failed` instead of a retry loop
                continue
            leased = await self._source_leases.claim(source)
            if leased is not None:  # None => lost the etag race to another sync
                claimed.append(leased)
        return claimed

    async def get_source_content(self, user_id: str, source_id: str) -> SourceContent:
        source = await self._rt.repos.sources.get_source(source_id, user_id)
        if source is None:
            raise SyncError("source not found")
        pages = [
            PageContent(
                index=p.index,
                filename=p.filename,
                content_type=p.content_type,
                read_url=await self._rt.blob.read_url(p.blob_path),
            )
            for p in source.page_list()
        ]
        return SourceContent(
            source_id=source.id,
            filename=source.filename,
            content_type=source.content_type,
            read_url=pages[0].read_url,
            pages=pages,
        )

    async def store_extraction(
        self,
        user_id: str,
        source_id: str,
        markdown: str,
        confidence: float,
        model_id: str | None = None,
        supersede: bool = False,
        client_label: str | None = None,
    ) -> Extraction:
        """Persist an extraction and move the source's active pointer (LLD §7.7).

        Stamp-before-move ordering: the new extraction is written, then the
        previous active one is marked superseded, then the source's pointer
        moves. A crash between steps leaves the old active extraction canonical
        and replayable.

        A source has one active read (BE-05): the pointer always moves, so the
        previous active read is marked superseded even when `supersede` is false
        (before BE-05 it stayed `active` beside the new one). `supersede` is still
        the agent's statement that this is a re-extraction; concept
        reconciliation on supersede is not implemented yet (docs/GAPS.md).
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
            client_label=client_label,
        )
        extraction = await self._rt.repos.extractions.create(extraction)

        history = await self._rt.repos.extractions.history_for_source(source_id)
        for previous in history:
            if previous.id != extraction.id and previous.status == ExtractionStatus.ACTIVE:
                previous.status = ExtractionStatus.SUPERSEDED
                previous.superseded_by = extraction.id
                await self._rt.repos.extractions.replace(previous, etag=previous.etag)

        # Retry on a lost etag race (e.g. the app asked to read again meanwhile)
        # so the pointer always moves to the new read.
        for _ in range(5):
            if await self._source_leases.release_processed(source, extraction.id) is not None:
                break
            source = await self._rt.repos.sources.get_source(source_id, user_id)
            if source is None:
                break
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
        read_of: dict[str, Optional[str]] = {}  # source_id -> its active extraction id
        touched: dict[tuple[str, str], list[ExtractionConcept]] = {}
        for c in concepts:
            extraction_id = None
            if c.source_id:
                if c.source_id not in read_of:
                    src = await self._rt.repos.sources.get_source(c.source_id, user_id)
                    read_of[c.source_id] = src.active_extraction_id if src else None
                extraction_id = read_of[c.source_id]
            concept = Concept(
                track_id=c.track_id,
                source_id=c.source_id,
                extraction_id=extraction_id,
                text=c.text,
                title=(c.title or "").strip()[:120] or None,
                # The concept's coverage key is its agenda concept_key.
                syllabus_ref=c.effective_key(),
                status=ConceptStatus.ACTIVE,
            )
            concept = await self._rt.repos.concepts.create(concept)
            created_concepts.append(concept)
            if c.source_id and extraction_id:
                touched.setdefault((c.source_id, extraction_id), []).append(
                    ExtractionConcept(
                        concept_id=concept.id,
                        title=(c.title or c.text).strip()[:120],
                        # Every logged concept is new until supersede reconciles
                        # against the previous read (docs/GAPS.md).
                        is_new=True,
                    )
                )
            # New card: due now so the material enters the review rotation.
            state = ConceptState(
                user_id=user_id,
                concept_id=concept.id,
                track_id=c.track_id,
                due_at=_now(),
            )
            await self._rt.repos.concept_states.create(state)

        for (source_id, extraction_id), items in touched.items():
            await self._update_read(
                source_id, extraction_id, lambda e, items=items: e.concepts.extend(items)
            )

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
        if created:
            await self._count_questions(user_id, concept_id, len(created))
        return created

    async def _count_questions(self, user_id: str, concept_id: str, n: int) -> None:
        """Add `n` to `questions_written` on the read the concept came from.

        The concept is found through the caller's card (user partition), which
        names its track (the concepts partition); a concept with no card, or not
        logged from a read, counts nowhere.
        """
        state = await self._rt.repos.concept_states.for_concept(user_id, concept_id)
        if state is None or not state.track_id:
            return
        concept = await self._rt.repos.concepts.get_concept(concept_id, state.track_id)
        if concept is None or not concept.source_id or not concept.extraction_id:
            return

        def bump(e: Extraction) -> None:
            e.questions_written += n

        await self._update_read(concept.source_id, concept.extraction_id, bump)

    async def _update_read(self, source_id: str, extraction_id: str, change) -> None:
        """Etag-guarded read-modify-write of one extraction's counters."""
        for _ in range(5):
            read = await self._rt.repos.extractions.get_extraction(extraction_id, source_id)
            if read is None:
                return
            change(read)
            if await self._rt.repos.extractions.replace(read, etag=read.etag) is not None:
                return

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
                    stem=question.stem if question else "",
                    options=list(question.options) if question else [],
                    correct_index=question.correct_index if question else 0,
                    question_version=question.version if question else review.question_version,
                    selected_index=review.selected_index,
                    is_correct=review.is_correct,
                )
            )
        return out

    async def submit_gradings(self, user_id: str, gradings: list[GradingInput]) -> int:
        """Apply semantic grades idempotently and re-fold each affected card inline.

        Returns the number of gradings newly applied (idempotent no-ops excluded).
        BE-06: delegates to `GradingService`, which also stores question
        rewrites (decision D8) and recomputes mastery.
        """
        return (await GradingService(self._rt).submit(user_id, gradings)).applied

    async def _recompute_card(self, user_id: str, concept_id: str) -> ConceptState:
        return await ReviewService(self._rt).recompute_card(user_id, concept_id)
