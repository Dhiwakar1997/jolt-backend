"""The review loop: sessions, two-stage review, inline FSRS, mastery (LLD §7.3–7.5, BE-06).

* **Sessions.** `start_session` builds an ordered queue of due questions: due
  concepts most overdue first, interleaving tracks, one question per concept
  (the concept's active questions are served in rotation). `end_session`
  summarises what the session's answers did, including **what moved** (old and
  new next-review dates).
* **Two stages.** Stage 1 locks the learner's own words and only then releases
  the options (the server-side stage lock: no other route returns options).
  Stage 2 scores the choice, sets the provisional grade and folds FSRS over the
  card's full history in the same request — no job, no deferral. Both stages are
  idempotent on a client request id.
* **Mastery.** Every fold also recomputes `mastered_at`; whether a mastered
  concept leaves the queue depends on the user's `retire_mastered` setting
  (`jolt.domain.mastery`).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from jolt.domain.grading import score_stage_two
from jolt.domain.mastery import is_retired, mastered_at
from jolt.domain.models import (
    Concept,
    ConceptState,
    Question,
    Review,
    ReviewSession,
    ReviewStatus,
    Track,
    UserSettings,
)
from jolt.domain.scheduling import fold_history
from jolt.runtime import Runtime

DAY = timedelta(days=1)
# How many due cards a session start scans before filtering and interleaving.
# Single-partition and ordered by due date, so this is a bound, not a cost.
_DUE_SCAN = 1000
# Days ahead the session summary counts ("Tomorrow, 5 questions").
UPCOMING_DAYS = 7


class ReviewError(Exception):
    """A request the review flow rejects (400)."""


class ReviewNotFound(ReviewError):
    """An id that does not exist for this user (404)."""


class ReviewConflict(ReviewError):
    """A client request id reused for a different request (409)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def stage_one_review_id(user_id: str, client_request_id: str) -> str:
    """Deterministic review id for an idempotent stage 1 (same user + key → same review)."""
    return hashlib.sha256(f"stage1:{user_id}:{client_request_id}".encode()).hexdigest()[:32]


def interval_days(due_at: Optional[datetime], since: datetime) -> int:
    """Whole days from `since` to the next review, at least 1 ("Back in 7 days")."""
    if due_at is None:
        return 1
    return max(1, round((due_at - since) / DAY))


def grading_of(review: Review) -> str:
    return "graded" if review.status == ReviewStatus.GRADED else "provisional"


def interleave(states: list[ConceptState]) -> list[ConceptState]:
    """Most overdue first, alternating tracks (BE-06 §1).

    Cards are grouped by track, each group most overdue first. Each round takes
    the head of every group, the most overdue head first, so a track with many
    overdue cards cannot crowd out the others.
    """
    far = datetime.max.replace(tzinfo=timezone.utc)
    ordered = sorted(states, key=lambda s: s.due_at or far)
    lanes: dict[str, list[ConceptState]] = {}
    for state in ordered:
        lanes.setdefault(state.track_id or "", []).append(state)
    queues = list(lanes.values())
    out: list[ConceptState] = []
    while queues:
        queues.sort(key=lambda q: q[0].due_at or far)
        for q in queues:
            out.append(q.pop(0))
        queues = [q for q in queues if q]
    return out


# --------------------------------------------------------------------------- #
# Results crossing the route boundary
# --------------------------------------------------------------------------- #
@dataclass
class DueItem:
    concept_id: str
    question_id: str
    stem: str


@dataclass
class QueueItem:
    concept_id: str
    concept_title: str
    question_id: str
    question_type: str  # "free_text" (BE-09 adds "pattern_match")
    stem: str
    track_id: str
    track_name: str
    version: int
    interval_days: Optional[int]
    review_number: int
    rewritten: bool


@dataclass
class SessionView:
    id: str
    started_at: datetime
    track_id: Optional[str]
    items: list[QueueItem]
    done_today: int


@dataclass
class StageOneResult:
    review_id: str
    options: list[str]  # released only now
    locked_at: datetime


@dataclass
class StageTwoResult:
    correct: bool
    expected_answer: str
    correct_index: int
    due_at: Optional[datetime]
    grading: str
    provisional_grade: int
    interval_days: int
    retired: bool
    feedback: Optional[str]


@dataclass
class ConceptRef:
    concept_id: str
    title: str


@dataclass
class Moved:
    concept_id: str
    title: str
    old_due_at: Optional[datetime]
    new_due_at: Optional[datetime]


@dataclass
class UpcomingDay:
    date: date
    questions: int


@dataclass
class SessionSummary:
    session_id: str
    started_at: datetime
    ended_at: datetime
    reviewed: int
    correct: int
    provisional: int
    graded: int
    retired: list[ConceptRef] = field(default_factory=list)
    moved: list[Moved] = field(default_factory=list)
    upcoming: list[UpcomingDay] = field(default_factory=list)


class ReviewService:
    def __init__(self, rt: Runtime) -> None:
        self._rt = rt
        self._concepts: dict[str, Optional[Concept]] = {}
        self._tracks: dict[str, Optional[Track]] = {}
        self._questions: dict[str, list[Question]] = {}

    # -- settings and lookups -------------------------------------------------
    async def user_settings(self, user_id: str) -> UserSettings:
        user = await self._rt.repos.users.get_user(user_id)
        return user.settings if user is not None else UserSettings()

    async def _concept(self, state: ConceptState) -> Optional[Concept]:
        if state.concept_id not in self._concepts:
            concept = None
            if state.track_id:
                concept = await self._rt.repos.concepts.get_concept(
                    state.concept_id, state.track_id
                )
            self._concepts[state.concept_id] = concept
        return self._concepts[state.concept_id]

    async def _track(self, user_id: str, track_id: str) -> Optional[Track]:
        """The caller's track, or a curated one."""
        if track_id not in self._tracks:
            track = await self._rt.repos.tracks.get_track(track_id, user_id)
            if track is None:
                track = await self._rt.repos.tracks.get_track(track_id, None)
            self._tracks[track_id] = track
        return self._tracks[track_id]

    @staticmethod
    def concept_title(concept: Optional[Concept], fallback: str) -> str:
        if concept is None:
            return fallback
        if concept.title and concept.title.strip():
            return concept.title.strip()
        first = concept.text.strip().splitlines()[0] if concept.text.strip() else fallback
        return first if len(first) <= 80 else first[:79].rstrip() + "…"

    async def _title(self, state: ConceptState) -> str:
        return self.concept_title(await self._concept(state), state.concept_id)

    async def _active_questions(self, concept_id: str) -> list[Question]:
        if concept_id not in self._questions:
            active = await self._rt.repos.questions.list_for_concept(concept_id)
            self._questions[concept_id] = sorted(active, key=lambda q: (q.created_at, q.id))
        return self._questions[concept_id]

    @staticmethod
    def pick_question(active: list[Question], state: ConceptState) -> Optional[Question]:
        """The question a session serves for a concept: its active ones in rotation."""
        if not active:
            return None
        return active[state.reps % len(active)]

    async def owned_state(self, user_id: str, concept_id: str) -> ConceptState:
        """The caller's card for a concept; a concept without one is not theirs."""
        state = await self._rt.repos.concept_states.for_concept(user_id, concept_id)
        if state is None:
            raise ReviewNotFound("concept not found")
        return state

    # -- due ------------------------------------------------------------------
    async def due_states(
        self,
        user_id: str,
        *,
        track_id: Optional[str] = None,
        settings: Optional[UserSettings] = None,
    ) -> list[ConceptState]:
        """Due cards, most overdue first, without retired (mastered) concepts."""
        settings = settings or await self.user_settings(user_id)
        states = await self._rt.repos.concept_states.due(user_id, _now(), limit=_DUE_SCAN)
        return [
            s
            for s in states
            if not is_retired(s, settings.retire_mastered)
            and (track_id is None or s.track_id == track_id)
        ]

    async def due(self, user_id: str, limit: int = 100) -> list[DueItem]:
        """`GET /v1/reviews/due` (LLD §7.5): every active question of each due card."""
        items: list[DueItem] = []
        for state in (await self.due_states(user_id))[: max(0, limit)]:
            for q in await self._active_questions(state.concept_id):
                items.append(DueItem(concept_id=q.concept_id, question_id=q.id, stem=q.stem))
        return items

    # -- sessions -------------------------------------------------------------
    async def start_session(
        self,
        user_id: str,
        *,
        track_id: Optional[str] = None,
        limit: int = 100,
        concept_id: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> SessionView:
        settings = await self.user_settings(user_id)
        if track_id is not None and await self._track(user_id, track_id) is None:
            raise ReviewNotFound("track not found")

        picked: list[tuple[ConceptState, Question]] = []
        if concept_id is not None:
            # "Practise this concept": its next question, due or not, retired or not.
            state = await self.owned_state(user_id, concept_id)
            question = self.pick_question(await self._active_questions(concept_id), state)
            if question is not None:
                picked.append((state, question))
        elif kind != "dsa_patterns":  # no DSA tracks before BE-09
            due = await self.due_states(user_id, track_id=track_id, settings=settings)
            for state in interleave(due):
                if len(picked) >= limit:
                    break
                question = self.pick_question(await self._active_questions(state.concept_id), state)
                if question is not None:  # a concept with no question cannot be reviewed
                    picked.append((state, question))

        session = await self._rt.repos.review_sessions.create(
            ReviewSession(
                user_id=user_id,
                track_id=track_id,
                concept_id=concept_id,
                kind=kind,
                limit=limit,
                question_ids=[q.id for _, q in picked],
                concept_ids=[s.concept_id for s, _ in picked],
            )
        )
        now = _now()
        items = [await self._queue_item(user_id, s, q, now) for s, q in picked]
        return SessionView(
            id=session.id,
            started_at=session.created_at,
            track_id=track_id,
            items=items,
            done_today=await self.done_today(user_id),
        )

    async def _queue_item(
        self, user_id: str, state: ConceptState, question: Question, now: datetime
    ) -> QueueItem:
        track = await self._track(user_id, state.track_id) if state.track_id else None
        last = state.last_review_at
        return QueueItem(
            concept_id=state.concept_id,
            concept_title=await self._title(state),
            question_id=question.id,
            question_type="free_text",
            stem=question.stem,
            track_id=state.track_id or "",
            track_name=track.name if track else "",
            version=question.version,
            interval_days=max(1, round((now - last) / DAY)) if last else None,
            review_number=state.reps + 1,
            rewritten=question.version > 1 and last is not None and question.created_at > last,
        )

    async def done_today(self, user_id: str) -> int:
        """Answers (stage 2) given since midnight UTC."""
        midnight = _now().replace(hour=0, minute=0, second=0, microsecond=0)
        reviews = await self._rt.repos.reviews.created_since(user_id, midnight)
        return sum(1 for r in reviews if r.selected_index is not None)

    async def end_session(self, user_id: str, session_id: str) -> SessionSummary:
        session = await self._rt.repos.review_sessions.get_session(session_id, user_id)
        if session is None:
            raise ReviewNotFound("session not found")
        if session.ended_at is None:
            session.ended_at = _now()
            saved = await self._rt.repos.review_sessions.replace(session, etag=session.etag)
            if saved is None:  # ended concurrently: use the stored end time
                session = await self._rt.repos.review_sessions.get_session(session_id, user_id)
            else:
                session = saved

        settings = await self.user_settings(user_id)
        answered = [
            r
            for r in await self._rt.repos.reviews.for_session(user_id, session_id)
            if r.selected_index is not None
        ]
        cards = await self._rt.repos.concept_states.all_for_user(user_id)
        states = {s.concept_id: s for s in cards}

        retired: list[ConceptRef] = []
        moved: dict[str, Moved] = {}
        for review in answered:
            state = states.get(review.concept_id)
            title = await self._title(state) if state else review.concept_id
            if review.retired_now and all(r.concept_id != review.concept_id for r in retired):
                retired.append(ConceptRef(concept_id=review.concept_id, title=title))
            if review.concept_id not in moved:
                moved[review.concept_id] = Moved(
                    concept_id=review.concept_id,
                    title=title,
                    old_due_at=review.due_before,
                    new_due_at=None,
                )
        for concept_id, item in moved.items():
            state = states.get(concept_id)
            if state is not None and not is_retired(state, settings.retire_mastered):
                item.new_due_at = state.due_at

        return SessionSummary(
            session_id=session.id,
            started_at=session.created_at,
            ended_at=session.ended_at,
            reviewed=len(answered),
            correct=sum(1 for r in answered if r.is_correct),
            provisional=sum(1 for r in answered if grading_of(r) == "provisional"),
            graded=sum(1 for r in answered if grading_of(r) == "graded"),
            retired=retired,
            moved=[m for m in moved.values() if m.old_due_at != m.new_due_at],
            upcoming=await self._upcoming(list(states.values()), settings),
        )

    async def _upcoming(
        self, states: list[ConceptState], settings: UserSettings
    ) -> list[UpcomingDay]:
        """Questions due on each of the next days (UTC dates), one per concept."""
        today = _now().date()
        days = [today + timedelta(days=i) for i in range(1, UPCOMING_DAYS + 1)]
        counts = {d: 0 for d in days}
        for state in states:
            if state.due_at is None or is_retired(state, settings.retire_mastered):
                continue
            day = state.due_at.astimezone(timezone.utc).date()
            if day in counts and await self._active_questions(state.concept_id):
                counts[day] += 1
        return [UpcomingDay(date=d, questions=counts[d]) for d in days]

    # -- stage 1 --------------------------------------------------------------
    async def submit_stage_one(
        self,
        user_id: str,
        concept_id: str,
        question_id: str,
        free_text: str,
        *,
        client_request_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> StageOneResult:
        question = await self._rt.repos.questions.get_question(question_id, concept_id)
        if question is None:
            raise ReviewNotFound("question not found")
        # Only the caller's concepts (they hold a card for it): GAPS issue 1.
        if await self._rt.repos.concept_states.for_concept(user_id, concept_id) is None:
            raise ReviewNotFound("question not found")
        if session_id is not None:
            if await self._rt.repos.review_sessions.get_session(session_id, user_id) is None:
                raise ReviewNotFound("session not found")

        review = Review(
            user_id=user_id,
            question_id=question_id,
            concept_id=concept_id,
            free_text=free_text,
            free_text_locked_at=_now(),
            status=ReviewStatus.PENDING_GRADING,
            session_id=session_id,
            question_version=question.version,
            stage1_request_id=client_request_id,
        )
        if client_request_id:
            # Same key → same review id: a retry returns the locked review,
            # and of two concurrent requests exactly one creates it.
            review.id = stage_one_review_id(user_id, client_request_id)
            stored, created = await self._rt.repos.reviews.create_if_absent(
                review, partition_value=user_id
            )
            if not created and (
                stored.question_id != question_id or stored.concept_id != concept_id
            ):
                raise ReviewConflict("client_request_id was already used for another question")
        else:
            stored = await self._rt.repos.reviews.create(review)
        # Options released only now that stage 1 is locked.
        return StageOneResult(
            review_id=stored.id,
            options=list(question.options),
            locked_at=stored.free_text_locked_at or stored.created_at,
        )

    # -- stage 2 --------------------------------------------------------------
    async def submit_stage_two(
        self,
        user_id: str,
        review_id: str,
        selected_index: int,
        latency_ms: int | None,
        *,
        client_request_id: Optional[str] = None,
    ) -> StageTwoResult:
        for _ in range(3):
            review = await self._rt.repos.reviews.get_review(review_id, user_id)
            if review is None:
                raise ReviewError("review not found")
            if review.free_text_locked_at is None:
                raise ReviewError("stage 1 not submitted for this review")
            question = await self._rt.repos.questions.get_question(
                review.question_id, review.concept_id
            )
            if question is None:
                raise ReviewError("question not found")
            if review.selected_index is not None:
                if client_request_id and client_request_id == review.stage2_request_id:
                    return await self._replay_stage_two(user_id, review, question)
                raise ReviewError("stage 2 already submitted")
            if not 0 <= selected_index < len(question.options):
                raise ReviewError("selected_index is out of range")

            settings = await self.user_settings(user_id)
            before = await self._rt.repos.concept_states.for_concept(user_id, review.concept_id)
            score_stage_two(review, question, selected_index, latency_ms)
            review.stage2_request_id = client_request_id
            review.stage2_at = _now()
            review.due_before = before.due_at if before else None
            saved = await self._rt.repos.reviews.replace(review, etag=review.etag)
            if saved is None:
                continue  # a concurrent stage 2 won: re-read and replay or reject

            state = await self.recompute_card(user_id, review.concept_id)
            was_retired = before is not None and is_retired(before, settings.retire_mastered)
            retired = is_retired(state, settings.retire_mastered)

            def record(r: Review) -> None:
                r.due_after = state.due_at
                r.retired_now = retired and not was_retired

            saved = await self._patch_review(saved, record)
            return self._stage_two_result(saved, question, retired)
        raise ReviewError("stage 2 already submitted")

    async def _patch_review(self, review: Review, change: Callable[[Review], None]) -> Review:
        """Etag-guarded read-modify-write of one review."""
        current: Optional[Review] = review
        for _ in range(5):
            if current is None:
                break
            change(current)
            saved = await self._rt.repos.reviews.replace(current, etag=current.etag)
            if saved is not None:
                return saved
            current = await self._rt.repos.reviews.get_review(review.id, review.user_id)
        return review

    async def _replay_stage_two(
        self, user_id: str, review: Review, question: Question
    ) -> StageTwoResult:
        settings = await self.user_settings(user_id)
        state = await self._rt.repos.concept_states.for_concept(user_id, review.concept_id)
        retired = state is not None and is_retired(state, settings.retire_mastered)
        return self._stage_two_result(review, question, retired)

    @staticmethod
    def _stage_two_result(review: Review, question: Question, retired: bool) -> StageTwoResult:
        graded = grading_of(review) == "graded"
        return StageTwoResult(
            correct=bool(review.is_correct),
            expected_answer=question.expected_answer,
            correct_index=question.correct_index,
            due_at=review.due_after,
            grading=grading_of(review),
            provisional_grade=review.provisional_fsrs_grade or 1,
            interval_days=interval_days(review.due_after, review.stage2_at or _now()),
            retired=retired,
            feedback=review.feedback if graded else None,
        )

    # -- FSRS -----------------------------------------------------------------
    async def recompute_card(self, user_id: str, concept_id: str) -> ConceptState:
        """Fold FSRS over the card's full history and persist — inline (LLD §7.4).

        Also recomputes `mastered_at` (BE-06 §5). Etag-guarded; a lost race
        re-reads and re-folds.
        """
        threshold = self._rt.settings.mastery_stability_days
        folded: Optional[ConceptState] = None
        for _ in range(3):
            history = await self._rt.repos.reviews.history_for_concept(user_id, concept_id)
            state = await self._rt.repos.concept_states.for_concept(user_id, concept_id)
            if state is None:
                state = await self._rt.repos.concept_states.create(
                    ConceptState(user_id=user_id, concept_id=concept_id)
                )
            folded = fold_history(state, history)
            folded.id = state.id
            folded.etag = state.etag
            folded.track_id = state.track_id
            folded.created_at = state.created_at
            folded.unretired_at = state.unretired_at
            folded.mastered_at = mastered_at(folded, history, threshold)
            saved = await self._rt.repos.concept_states.replace(folded, etag=state.etag)
            if saved is not None:
                return saved
        assert folded is not None
        return folded

    # -- concepts -------------------------------------------------------------
    async def unretire(self, user_id: str, concept_id: str) -> ConceptState:
        """Bring a retired concept back into the queue, due now.

        A no-op for a concept that is not retired. The concept is retired again
        only by a later review that is still mastered.
        """
        for _ in range(5):
            state = await self.owned_state(user_id, concept_id)
            settings = await self.user_settings(user_id)
            if not is_retired(state, settings.retire_mastered):
                return state
            now = _now()
            state.unretired_at = now
            state.due_at = now
            saved = await self._rt.repos.concept_states.replace(state, etag=state.etag)
            if saved is not None:
                return saved
        raise ReviewConflict("the concept changed while unretiring; try again")

    async def question_versions(
        self, user_id: str, concept_id: str
    ) -> tuple[list[Question], Optional[Question]]:
        """Every question version of a concept, oldest first, and the current one."""
        state = await self.owned_state(user_id, concept_id)
        every = await self._rt.repos.questions.list_for_concept(concept_id, active_only=False)
        every.sort(key=lambda q: (q.created_at, q.version, q.id))
        current = self.pick_question(await self._active_questions(concept_id), state)
        return every, current

    async def get_review(self, user_id: str, review_id: str) -> tuple[Review, Optional[Question]]:
        review = await self._rt.repos.reviews.get_review(review_id, user_id)
        if review is None:
            raise ReviewNotFound("review not found")
        question = await self._rt.repos.questions.get_question(
            review.question_id, review.concept_id
        )
        return review, question
