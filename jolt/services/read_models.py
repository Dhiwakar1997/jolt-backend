"""Read models for the Tracks, Track detail and Concept screens (BE-07).

Each screen is served by one call whose Cosmos cost is a fixed handful of
single-partition reads, independent of how many concepts, questions or sources
it shows (no N+1):

* Tracks (`GET /v1/tracks`): the caller's tracks, the curated ones, the user
  document (settings), all of the caller's cards, the caller's sources.
* Track detail (`GET /v1/tracks/{id}`): the track, the user document, the
  track's concepts, the caller's cards on it, the caller's sources.
* Concept (`GET /v1/concepts/{id}`): the card, the concept, its track, its
  question versions, its review history, its source, the user document.

What would otherwise need a query per row lives on the card (`jolt.services.
cards`): the active questions in rotation order, the newest version and the
last answer. Cards written before BE-07 lack them and are backfilled the first
time a list reads them (one query each, once).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from jolt.domain.coverage import is_off_plan
from jolt.domain.mastery import is_retired
from jolt.domain.models import (
    AgendaSource,
    Concept,
    ConceptState,
    ConceptStatus,
    CorrectionNote,
    Question,
    QuestionStatus,
    Review,
    Source,
    Track,
    TrackOrigin,
    UserSettings,
)
from jolt.domain.scheduling import predicted_recall
from jolt.runtime import Runtime
from jolt.services.cards import current_question_id, refresh_card_questions, rotation
from jolt.services.ownership import NotOwned, owned_concept, visible_track
from jolt.services.reviews import ReviewService, grading_of

DAY = timedelta(days=1)
RECENT_SOURCES = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Card rules shared by the screens
# --------------------------------------------------------------------------- #
def concept_state(state: ConceptState, now: datetime) -> str:
    """new (never answered) / mastered / due / learning."""
    if state.reps == 0 and state.last_review_at is None:
        return "new"
    if state.mastered_at is not None:
        return "mastered"
    if state.due_at is not None and state.due_at <= now:
        return "due"
    return "learning"


def is_due(state: ConceptState, now: datetime, settings: UserSettings) -> bool:
    """In the review queue right now: due, not retired, not superseded, has a question."""
    return (
        state.superseded_at is None
        and state.due_at is not None
        and state.due_at <= now
        and not is_retired(state, settings.retire_mastered)
        # None: a card from before BE-07 not yet backfilled; assume it has one.
        and (state.active_question_ids is None or len(state.active_question_ids) > 0)
    )


def next_review(state: ConceptState, settings: UserSettings) -> Optional[datetime]:
    if state.superseded_at is not None or is_retired(state, settings.retire_mastered):
        return None
    return state.due_at


def mean_recall(states: list[ConceptState], now: datetime) -> float:
    values = [r for s in states if (r := predicted_recall(s, now)) is not None]
    return round(sum(values) / len(values), 4) if values else 0.0


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #
@dataclass
class TrackSummary:
    track: Track
    concept_count: int = 0
    due_count: int = 0
    source_count: int = 0
    mastered_count: int = 0
    retention: float = 0.0
    enrolled: bool = False


@dataclass
class FoundIn:
    source_id: str
    source_title: str
    uploaded_at: datetime
    page: Optional[int] = None


@dataclass
class TrackItem:
    concept_key: str
    label: str
    parent: Optional[str]
    source: str
    off_plan: bool
    concept_id: Optional[str]
    concept_title: Optional[str]
    state: str
    next_review_at: Optional[datetime]
    question_count: int
    question_version: Optional[int]
    found_in: Optional[FoundIn]


@dataclass
class DueToday:
    concept_id: str
    title: str
    question_id: str
    last_answer: Optional[str]


@dataclass
class RecentSource:
    id: str
    title: str
    kind: str
    page_count: int
    uploaded_at: datetime


@dataclass
class TrackDetail(TrackSummary):
    items: list[TrackItem] = field(default_factory=list)
    due_today: list[DueToday] = field(default_factory=list)
    recent_sources: list[RecentSource] = field(default_factory=list)
    off_plan_count: int = 0


@dataclass
class TimelineEntry:
    review_id: str
    reviewed_at: datetime
    free_text: str
    is_correct: Optional[bool]
    grade: Optional[int]
    grading: str
    feedback: Optional[str]
    question_id: str
    question_version: int
    interval_days: Optional[int]


@dataclass
class FsrsView:
    stability: float
    difficulty: float
    retrievability: float
    due_at: Optional[datetime]
    reps: int
    lapses: int


@dataclass
class ConceptDetail:
    concept: Concept
    track_name: str
    title: str
    state: str
    retired: bool
    off_plan: bool
    found_in: Optional[FoundIn]
    timeline: list[TimelineEntry]
    question_versions: list[Question]
    current_question: Optional[Question]
    adaptive_note: Optional[str]
    fsrs: FsrsView
    pending_corrections: list[CorrectionNote]


def _source_title(source: Source) -> str:
    return source.title or source.filename


def _found_in(concept: Concept, sources: dict[str, Source]) -> Optional[FoundIn]:
    source = sources.get(concept.source_id) if concept.source_id else None
    if source is None:
        return None
    return FoundIn(
        source_id=source.id,
        source_title=_source_title(source),
        uploaded_at=source.confirmed_at or source.created_at,
        page=concept.page,
    )


class ReadModelService:
    def __init__(self, rt: Runtime) -> None:
        self._rt = rt

    async def _settings(self, user_id: str, given: Optional[UserSettings]) -> UserSettings:
        # Routes pass the settings of the user document auth already loaded.
        return given if given is not None else await ReviewService(self._rt).user_settings(user_id)

    async def _backfill(self, user_id: str, states: list[ConceptState]) -> list[ConceptState]:
        """Fill the question fields of cards written before BE-07 (once per card)."""
        out: list[ConceptState] = []
        for state in states:
            if state.active_question_ids is None and state.superseded_at is None:
                state = await refresh_card_questions(self._rt, user_id, state.concept_id) or state
            out.append(state)
        return out

    # -- Tracks ---------------------------------------------------------------
    @staticmethod
    def summarise(
        track: Track,
        cards: list[ConceptState],
        source_count: int,
        settings: UserSettings,
        now: datetime,
    ) -> TrackSummary:
        live = [c for c in cards if c.superseded_at is None]
        archived = track.archived_at is not None
        return TrackSummary(
            track=track,
            concept_count=len(live),
            # An archived track is paused: its concepts are out of the queue.
            due_count=0 if archived else sum(1 for c in live if is_due(c, now, settings)),
            source_count=source_count,
            mastered_count=sum(1 for c in live if c.mastered_at is not None),
            retention=mean_recall(live, now),
            enrolled=track.origin == TrackOrigin.JOLT and bool(live),
        )

    async def list_tracks(
        self, user_id: str, settings: Optional[UserSettings] = None
    ) -> list[TrackSummary]:
        own = await self._rt.repos.tracks.list_for_user(user_id)
        curated = await self._rt.repos.tracks.list_curated()
        settings = await self._settings(user_id, settings)
        cards = await self._rt.repos.concept_states.all_for_user(user_id)
        sources = await self._rt.repos.sources.list_confirmed(user_id)
        now = _now()
        by_track: dict[str, list[ConceptState]] = {}
        for card in cards:
            by_track.setdefault(card.track_id or "", []).append(card)
        source_counts: dict[str, int] = {}
        for s in sources:
            if s.track_id:
                source_counts[s.track_id] = source_counts.get(s.track_id, 0) + 1
        return [
            self.summarise(t, by_track.get(t.id, []), source_counts.get(t.id, 0), settings, now)
            for t in own + curated
        ]

    async def track_summary(
        self, user_id: str, track: Track, settings: Optional[UserSettings] = None
    ) -> TrackSummary:
        settings = await self._settings(user_id, settings)
        cards = await self._rt.repos.concept_states.for_track(user_id, track.id)
        sources = await self._rt.repos.sources.list_confirmed(user_id, track_id=track.id)
        return self.summarise(track, cards, len(sources), settings, _now())

    # -- Track detail ---------------------------------------------------------
    async def track_detail(
        self, user_id: str, track_id: str, settings: Optional[UserSettings] = None
    ) -> TrackDetail:
        track = await visible_track(self._rt, user_id, track_id)
        settings = await self._settings(user_id, settings)
        cards = await self._rt.repos.concept_states.for_track(user_id, track.id)
        concepts = await self._rt.repos.concepts.list_for_track(track.id)
        all_sources = await self._rt.repos.sources.list_confirmed(user_id)
        now = _now()

        cards = await self._backfill(user_id, cards)
        state_of = {c.concept_id: c for c in cards if c.superseded_at is None}
        mine = sorted((c for c in concepts if c.id in state_of), key=lambda c: (c.created_at, c.id))
        sources = {s.id: s for s in all_sources}
        track_sources = [s for s in all_sources if s.track_id == track.id]  # newest first

        summary = self.summarise(track, cards, len(track_sources), settings, now)
        detail = TrackDetail(**summary.__dict__)

        def row(concept: Concept, *, key: str, label: str, parent, source: str) -> TrackItem:
            state = state_of[concept.id]
            return TrackItem(
                concept_key=key,
                label=label,
                parent=parent,
                source=source,
                off_plan=is_off_plan(track, concept),
                concept_id=concept.id,
                concept_title=ReviewService.concept_title(concept, concept.id),
                state=concept_state(state, now),
                next_review_at=next_review(state, settings),
                question_count=len(state.active_question_ids or []),
                question_version=state.question_version,
                found_in=_found_in(concept, sources),
            )

        by_key: dict[str, list[Concept]] = {}
        for concept in mine:
            if concept.syllabus_ref:
                by_key.setdefault(concept.syllabus_ref, []).append(concept)
        placed: set[str] = set()
        for item in track.agenda.syllabus:
            source = track.agenda.item_source(item).value
            covering = by_key.get(item.concept_key, [])
            if not covering:
                detail.items.append(
                    TrackItem(
                        concept_key=item.concept_key,
                        label=item.label,
                        parent=item.parent,
                        source=source,
                        off_plan=False,
                        concept_id=None,
                        concept_title=None,
                        state="not_covered",
                        next_review_at=None,
                        question_count=0,
                        question_version=None,
                        found_in=None,
                    )
                )
                continue
            for concept in covering:
                if concept.id in placed:
                    continue  # a duplicate key in the syllabus: list the concept once
                placed.add(concept.id)
                detail.items.append(
                    row(
                        concept,
                        key=item.concept_key,
                        label=item.label,
                        parent=item.parent,
                        source=source,
                    )
                )
        # Concepts outside the syllabus: off-plan on a locked track (BE-07 §5),
        # otherwise not filed under any item (logged without a concept_key).
        for concept in mine:
            if concept.id in placed:
                continue
            title = ReviewService.concept_title(concept, concept.id)
            detail.items.append(
                row(
                    concept,
                    key=concept.syllabus_ref or concept.id,
                    label=title,
                    parent=None,
                    source=AgendaSource.EMERGENT.value,
                )
            )
        detail.off_plan_count = sum(1 for i in detail.items if i.off_plan)

        if track.archived_at is None:
            far = datetime.max.replace(tzinfo=timezone.utc)
            due = sorted(
                (c for c in mine if is_due(state_of[c.id], now, settings)),
                key=lambda c: state_of[c.id].due_at or far,
            )
            for concept in due:
                state = state_of[concept.id]
                question_id = current_question_id(state)
                if question_id is None:
                    continue
                detail.due_today.append(
                    DueToday(
                        concept_id=concept.id,
                        title=ReviewService.concept_title(concept, concept.id),
                        question_id=question_id,
                        last_answer=state.last_answer,
                    )
                )
        detail.recent_sources = [
            RecentSource(
                id=s.id,
                title=_source_title(s),
                kind=s.kind,
                page_count=len(s.page_list()),
                uploaded_at=s.confirmed_at or s.created_at,
            )
            for s in track_sources[:RECENT_SOURCES]
        ]
        return detail

    # -- Concept --------------------------------------------------------------
    async def concept_detail(
        self, user_id: str, concept_id: str, settings: Optional[UserSettings] = None
    ) -> ConceptDetail:
        concept, state = await owned_concept(self._rt, user_id, concept_id)
        try:
            track: Optional[Track] = await visible_track(self._rt, user_id, concept.track_id)
        except NotOwned:
            track = None
        settings = await self._settings(user_id, settings)
        versions = await self._rt.repos.questions.list_for_concept(concept_id, active_only=False)
        versions.sort(key=lambda q: (q.created_at, q.version, q.id))
        history = await self._rt.repos.reviews.history_for_concept(user_id, concept_id)
        source = (
            await self._rt.repos.sources.get_source(concept.source_id, user_id)
            if concept.source_id
            else None
        )
        now = _now()

        active = rotation([q for q in versions if q.status == QuestionStatus.ACTIVE])
        current = active[state.reps % len(active)] if active else None
        if concept.status == ConceptStatus.SUPERSEDED:
            current = None

        return ConceptDetail(
            concept=concept,
            track_name=track.name if track else "",
            title=ReviewService.concept_title(concept, concept.id),
            state=concept_state(state, now),
            retired=is_retired(state, settings.retire_mastered),
            off_plan=bool(track) and is_off_plan(track, concept),
            found_in=_found_in(concept, {source.id: source} if source else {}),
            timeline=self._timeline(history),
            question_versions=versions,
            current_question=current,
            adaptive_note=(
                current.rewrite_reason if current is not None and current.version > 1 else None
            ),
            fsrs=FsrsView(
                stability=state.stability,
                difficulty=state.difficulty,
                retrievability=round(predicted_recall(state, now) or 0.0, 4),
                due_at=next_review(state, settings),
                reps=state.reps,
                lapses=state.lapses,
            ),
            pending_corrections=list(state.corrections),
        )

    @staticmethod
    def _timeline(history: list[Review]) -> list[TimelineEntry]:
        """Every answer, oldest first, with the gap since the previous one."""
        out: list[TimelineEntry] = []
        previous: Optional[datetime] = None
        for r in history:
            if r.free_text_locked_at is None and r.free_text is None:
                continue
            at = r.free_text_locked_at or r.created_at
            graded = grading_of(r) == "graded"
            out.append(
                TimelineEntry(
                    review_id=r.id,
                    reviewed_at=at,
                    free_text=r.free_text or "",
                    is_correct=r.is_correct,
                    grade=r.final_fsrs_grade or r.provisional_fsrs_grade,
                    grading=grading_of(r),
                    feedback=r.feedback if graded else None,
                    question_id=r.question_id,
                    question_version=r.question_version,
                    interval_days=round((at - previous) / DAY) if previous else None,
                )
            )
            previous = at
        return out
