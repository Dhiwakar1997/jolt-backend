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
    AgendaStatus,
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
    TrackOrigin,
)
from jolt.domain.sync import (
    PlanCorrection,
    PlanGrading,
    PlanReread,
    PlanSource,
    PlanTrackInfo,
    SyncPlan,
    build_plan,
    upload_day,
)
from jolt.runtime import Runtime
from jolt.services.cards import refresh_card_questions
from jolt.services.corrections import CorrectionService
from jolt.services.gradings import GradingService
from jolt.services.ownership import NotOwned, owned_card, owned_track
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
    # BE-07: the 1-based page of the source the concept was found on.
    page: Optional[int] = None

    def effective_key(self) -> Optional[str]:
        return self.concept_key or self.syllabus_ref


@dataclass
class QuestionInput:
    stem: str
    options: list[str]
    correct_index: int
    expected_answer: str


@dataclass
class LoggedSession:
    """What `log_session` wrote. One session per upload date (decision D6)."""

    sessions: list[Session]  # oldest upload date first; a source-less session last
    concepts: list[Concept]  # input order
    # (track_id, concept_key) pairs this call added to an agenda.
    added_to_agenda: set[tuple[str, str]]
    # The caller's sources the call referenced, by id.
    sources: dict[str, Source]


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
    async def plan(
        self,
        user_id: str,
        *,
        claimed_ids: set[str] | frozenset[str] = frozenset(),
        limit: int = 50,
    ) -> SyncPlan:
        """The `jolt_sync` plan (BE-04). `claimed_ids` are the sources the caller's
        sync run already claimed: still-leased ones keep the run open, while
        sources leased by another client do not.

        Read-again requests (BE-05) are source work like any other (asking puts
        the source back to `unprocessed`); the ones this caller can take are also
        listed in `reread_sources` with their preferred model. Concepts with
        correction notes waiting (BE-07) are work too. A source whose
        lease lapsed `source_max_attempts` times is left out: the next claim marks
        it `failed`.
        """
        rows = await self._rt.repos.sources.list_unprocessed(user_id, limit)
        claimable: list[PlanSource] = []
        mine: list[PlanSource] = []
        rereads: list[PlanReread] = []
        elsewhere = 0
        for source in rows:
            item = PlanSource(
                source_id=source.id,
                filename=source.filename,
                track_id=source.track_id,
                uploaded_at=source.uploaded_at,
                lease_expires_at=source.lease_expires_at,
            )
            if self._is_claimable(source):
                if self._exhausted(source):
                    continue
                claimable.append(item)
            elif source.id in claimed_ids:
                mine.append(item)
            else:
                elsewhere += 1
                continue
            if source.reread_requested_at is not None:
                rereads.append(
                    PlanReread(
                        source_id=source.id,
                        title=source.title or source.filename,
                        filename=source.filename,
                        track_id=source.track_id,
                        preferred_model=source.reread_preferred_model,
                        requested_at=source.reread_requested_at,
                    )
                )

        gradings: list[PlanGrading] = []
        for review in await self._rt.repos.reviews.list_pending_gradings(user_id, limit):
            state = await self._rt.repos.concept_states.for_concept(user_id, review.concept_id)
            gradings.append(PlanGrading(review.id, state.track_id if state else None))

        # BE-07: concepts the learner flagged, waiting for jolt_correct_concept.
        # The same list jolt_get_pending_corrections returns, so `done` can be
        # reached by working through it.
        corrections = [
            PlanCorrection(p.concept_id, p.track_id)
            for p in await CorrectionService(self._rt).pending(user_id)
        ]

        tracks = await TrackService(self._rt).list_tracks(user_id)
        return build_plan(
            claimable=claimable,
            claimed_by_run=mine,
            in_flight_elsewhere=elsewhere,
            gradings=gradings,
            rereads=rereads,
            corrections=corrections,
            tracks={t.id: PlanTrackInfo(t.id, t.name, t.agenda.status.value) for t in tracks},
            user_track_ids=[t.id for t in tracks if t.origin == TrackOrigin.USER],
            lease_seconds=self._rt.settings.lease_ttl_seconds,
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
        (before BE-05 it stayed `active` beside the new one). Concept
        reconciliation runs after this, in `jolt.services.reconcile`, when the
        agent passes the read's concepts (BE-07).
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
    async def log_session(
        self,
        user_id: str,
        source_ids: list[str],
        concepts: list[ConceptInput],
        *,
        sync_run_id: Optional[str] = None,
    ) -> LoggedSession:
        """Create the concepts (each with a fresh, due-now FSRS state) and the
        session(s) they belong to.

        Decision D6: one session per upload date. Each concept goes to the session
        of its `source_id`'s upload date; a concept without a (known) source goes
        to the date of the first known source in `source_ids`. A call that spans
        three upload days therefore writes three sessions, oldest first; inside a
        sync run, later calls for a date already seen extend that date's session.

        Also grows each track's agenda: any concept whose `concept_key` is not yet
        in the track's agenda syllabus is auto-added (as a leaf, or under a supplied
        parent). Skipped on locked agendas. Auto-add is keyed on concept_key
        membership, so re-running a sync never duplicates entries.
        """
        # Ownership first (BE-07, A16): every track must be the caller's own and
        # every source this call mentions theirs, before anything is written.
        for track_id in dict.fromkeys(c.track_id for c in concepts):
            try:
                await owned_track(self._rt, user_id, track_id)
            except NotOwned as exc:
                raise SyncError(f"{exc}: {track_id}") from exc
        wanted = list(dict.fromkeys([*source_ids, *(c.source_id for c in concepts if c.source_id)]))
        sources: dict[str, Source] = {}
        for sid in wanted:
            source = await self._rt.repos.sources.get_source(sid, user_id)
            if source is None:
                raise SyncError(f"source not found: {sid}")
            sources[sid] = source

        def day_of(sid: Optional[str]) -> Optional[str]:
            source = sources.get(sid) if sid else None
            return upload_day(source.uploaded_at) if source else None

        fallback = next((d for d in map(day_of, source_ids) if d), None)
        groups: dict[Optional[str], dict[str, list[str]]] = {}

        def group(day: Optional[str]) -> dict[str, list[str]]:
            return groups.setdefault(day, {"source_ids": [], "concept_ids": []})

        for sid in source_ids:
            g = group(day_of(sid) or fallback)
            if sid not in g["source_ids"]:
                g["source_ids"].append(sid)

        created_concepts: list[Concept] = []
        # BE-05: a concept logged from a source is credited to that source's
        # active read (Past reads: "Found N concepts").
        touched: dict[tuple[str, str], list[ExtractionConcept]] = {}
        for c in concepts:
            source = sources.get(c.source_id) if c.source_id else None
            extraction_id = source.active_extraction_id if source else None
            concept = Concept(
                track_id=c.track_id,
                source_id=c.source_id,
                extraction_id=extraction_id,
                text=c.text,
                title=(c.title or "").strip()[:120] or None,
                # The concept's coverage key is its agenda concept_key.
                syllabus_ref=c.effective_key(),
                status=ConceptStatus.ACTIVE,
                page=c.page if isinstance(c.page, int) and c.page >= 1 else None,
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
                active_question_ids=[],
            )
            await self._rt.repos.concept_states.create(state)
            g = group(day_of(c.source_id) or fallback)
            g["concept_ids"].append(concept.id)
            if c.source_id and c.source_id not in g["source_ids"]:
                g["source_ids"].append(c.source_id)

        for (source_id, extraction_id), items in touched.items():
            await self._update_read(
                source_id, extraction_id, lambda e, items=items: e.concepts.extend(items)
            )

        added = await self._auto_add_agenda(user_id, concepts)

        if not groups:
            group(None)  # an empty call still records one (empty) session
        # Inside a sync run, a later call for the same upload date extends that
        # date's session instead of starting another one.
        existing: dict[Optional[str], Session] = {}
        if sync_run_id:
            for prior in await self._rt.repos.sessions.for_sync_run(user_id, sync_run_id):
                existing.setdefault(prior.upload_date, prior)
        sessions: list[Session] = []
        for day in sorted(groups, key=lambda d: (d is None, d or "")):
            g = groups[day]
            if day is not None and day in existing:
                sessions.append(await self._extend_session(existing[day], g))
                continue
            session = Session(
                user_id=user_id,
                source_ids=g["source_ids"],
                concept_ids=g["concept_ids"],
                upload_date=day,
                sync_run_id=sync_run_id,
            )
            sessions.append(await self._rt.repos.sessions.create(session))
        return LoggedSession(
            sessions=sessions,
            concepts=created_concepts,
            added_to_agenda=added,
            sources=sources,
        )

    async def _extend_session(self, session: Session, g: dict[str, list[str]]) -> Session:
        """Append sources and concepts to a session (etag-guarded, retried)."""
        for _ in range(4):
            session.source_ids += [s for s in g["source_ids"] if s not in session.source_ids]
            session.concept_ids += [c for c in g["concept_ids"] if c not in session.concept_ids]
            saved = await self._rt.repos.sessions.replace(session, etag=session.etag)
            if saved is not None:
                return saved
            fresh = await self._rt.repos.sessions.get(session.id, session.user_id)
            if fresh is None:
                break
            session = fresh
        raise SyncError("could not extend the session; retry jolt_log_session")

    async def _auto_add_agenda(
        self, user_id: str, concepts: list[ConceptInput]
    ) -> set[tuple[str, str]]:
        """Grow each involved track's agenda from the session's concepts.

        Grouped by track so each track's agenda is a single etag-guarded
        read-modify-write (serialised against a concurrent refine). Returns the
        (track_id, concept_key) pairs that were added.
        """
        by_track: dict[str, list[SyllabusItem]] = {}
        for c in concepts:
            key = c.effective_key()
            if not key:
                continue  # no agenda linkage → nothing to add
            by_track.setdefault(c.track_id, []).append(
                SyllabusItem(concept_key=key, label=c.title or key, parent=c.parent)
            )
        added: set[tuple[str, str]] = set()
        if not by_track:
            return added
        track_service = TrackService(self._rt)
        for track_id, items in by_track.items():
            try:
                before = await track_service.get_agenda(user_id, track_id)
                if before.status == AgendaStatus.LOCKED:
                    continue
                after = await track_service.auto_add_concepts(user_id, track_id, items)
            except TrackError:
                # A missing track shouldn't fail the whole session log.
                continue
            added |= {(track_id, k) for k in after.keys() - before.keys()}
        return added

    async def create_questions(
        self, user_id: str, concept_id: str, questions: list[QuestionInput]
    ) -> list[Question]:
        # Only the caller's concepts (they hold its card): BE-07, A16.
        try:
            await owned_card(self._rt, user_id, concept_id)
        except NotOwned as exc:
            raise SyncError(str(exc)) from exc
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
            await refresh_card_questions(self._rt, user_id, concept_id)
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
