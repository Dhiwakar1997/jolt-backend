"""Curated tracks: catalog, enrolment, track content and DSA sessions (BE-09, D7).

* **Catalog and enrolment.** Curated tracks (`origin: jolt`, `user_id: null`)
  are readable by everyone and writable by no client. A learner *enrols* to add
  one; the enrolment is kept on their user document. Their cards on the track
  are created lazily, by the first review. Leaving (unenrol) keeps the cards and
  reviews as history but takes the track's concepts out of the review queue.
* **Track content.** The DSA library (on each pattern concept) and the decision
  tree (on the track content document) are served as they are.
* **DSA sessions.** `draw` builds a session by the v1 rules
  (`jolt.domain.dsa.draw_session`) from the track content's problem index, with
  the learner's cards deciding which patterns come first.
* **The answer is withheld.** A `pattern_match` question's pattern is its
  answer, so wherever it is served before stage 2 its concept is masked: the
  queue item carries the *track* id as `concept_id` and a neutral title, and
  stage 1 resolves the real concept from the track content (`resolve_masked`).
"""

from __future__ import annotations

import hashlib
import random
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from jolt.domain import dsa
from jolt.domain.mastery import is_retired
from jolt.domain.models import (
    Concept,
    ConceptState,
    Enrolment,
    Question,
    QuestionStatus,
    QuestionType,
    Track,
    TrackContent,
    TrackKind,
    TrackOrigin,
    User,
    UserSettings,
)
from jolt.runtime import Runtime
from jolt.services.cards import refresh_card_questions

# The neutral title a masked `pattern_match` queue item carries.
PATTERN_MATCH_TITLE = "Pattern match"
# How far back an answered problem counts as "recent" for the draw.
RECENT_DAYS = 14


def _now() -> datetime:
    return datetime.now(timezone.utc)


class CuratedError(Exception):
    pass


class CuratedNotFound(CuratedError):
    """No curated track with that id (also: a learner's own track)."""


def is_pattern_match(question: Optional[Question]) -> bool:
    return question is not None and question.type == QuestionType.PATTERN_MATCH


def public_concept_id(question: Question, concept_id: str) -> str:
    """The concept id a client may see before stage 2 (masked for pattern_match)."""
    if is_pattern_match(question) and question.pattern_match is not None:
        return question.pattern_match.track_id
    return concept_id


def pattern_match_payload(question: Question) -> Optional[dict]:
    """What a queue item shows of a pattern_match problem before the answer: the
    description, the I/O contract and examples, difficulty and time weight.
    Never the title or the LeetCode link (both name the problem)."""
    pm = question.pattern_match
    if pm is None:
        return None
    contract = "\n".join(
        part
        for part in (
            f"Input: {pm.io_input}" if pm.io_input else "",
            f"Output: {pm.io_output}" if pm.io_output else "",
        )
        if part
    )
    return {
        "description": pm.description,
        "io_contract": contract,
        "io_input": pm.io_input,
        "io_output": pm.io_output,
        "examples": [
            {"input": e.input, "output": e.output, "note": e.explanation} for e in pm.examples
        ],
        "difficulty": pm.difficulty,
        "time_weight": pm.time_weight,
    }


@dataclass
class DrawnItem:
    state: ConceptState  # the learner's card, or an unsaved blank one
    question: Question


@dataclass
class DsaDraw:
    items: list[DrawnItem]
    budget_minutes: int
    time_weight_total: int


class CuratedService:
    def __init__(self, rt: Runtime) -> None:
        self._rt = rt

    # -- catalog and enrolment ------------------------------------------------
    async def curated_track(self, track_id: str) -> Track:
        track = await self._rt.repos.tracks.get_track(track_id, None)
        if track is None or track.origin != TrackOrigin.JOLT:
            raise CuratedNotFound("curated track not found")
        return track

    async def catalog(self) -> list[Track]:
        """Curated tracks a learner can add, oldest first (stable order)."""
        tracks = await self._rt.repos.tracks.list_curated()
        tracks = [t for t in tracks if t.archived_at is None]
        return sorted(tracks, key=lambda t: (t.created_at, t.id))

    async def enrol(self, user_id: str, track_id: str) -> Enrolment:
        """Idempotent: enrolling again returns the first enrolment."""
        await self.curated_track(track_id)
        for _ in range(5):
            user = await self._rt.repos.users.get_user(user_id)
            if user is None:
                raise CuratedNotFound("user not found")
            for e in user.enrolments:
                if e.track_id == track_id:
                    return e
            entry = Enrolment(track_id=track_id)
            user.enrolments = [*user.enrolments, entry]
            if await self._rt.repos.users.replace(user, etag=user.etag) is not None:
                return entry
        raise CuratedError("enrolment conflict; try again")

    async def unenrol(self, user_id: str, track_id: str) -> None:
        """Idempotent. Cards and reviews stay (history); the track leaves the queue."""
        await self.curated_track(track_id)
        for _ in range(5):
            user = await self._rt.repos.users.get_user(user_id)
            if user is None or track_id not in user.enrolled_track_ids():
                return
            user.enrolments = [e for e in user.enrolments if e.track_id != track_id]
            if await self._rt.repos.users.replace(user, etag=user.etag) is not None:
                return
        raise CuratedError("enrolment conflict; try again")

    async def paused_track_ids(self, user: Optional[User]) -> set[str]:
        """Curated tracks whose cards stay out of the queue: the ones not enrolled."""
        enrolled = user.enrolled_track_ids() if user else set()
        return {t.id for t in await self._rt.repos.tracks.list_curated() if t.id not in enrolled}

    # -- content --------------------------------------------------------------
    async def content(self, track_id: str) -> TrackContent:
        await self.curated_track(track_id)
        content = await self._rt.repos.track_content.get_content(track_id)
        if content is None:
            raise CuratedNotFound("this track has no content yet")
        return content

    async def library(self, track_id: str) -> tuple[Track, list[Concept], TrackContent]:
        """The track, its pattern concepts in syllabus order, and its content."""
        track = await self.curated_track(track_id)
        content = await self.content(track_id)
        concepts = [c for c in await self._rt.repos.concepts.list_for_track(track_id) if c.curated]
        order = {item.concept_key: i for i, item in enumerate(track.agenda.syllabus)}
        concepts.sort(key=lambda c: (order.get(c.syllabus_ref or "", len(order)), c.id))
        return track, concepts, content

    @staticmethod
    def problem_counts(content: Optional[TrackContent]) -> dict[str, int]:
        """Playable problems per pattern concept."""
        return dict(Counter(p.concept_id for p in content.problems)) if content else {}

    # -- answering --------------------------------------------------------------
    async def resolve_masked(
        self, user: User, track_id: str, question_id: str
    ) -> Optional[Question]:
        """A pattern_match question addressed by its masked concept (the track id)."""
        if track_id not in user.enrolled_track_ids():
            return None
        content = await self._rt.repos.track_content.get_content(track_id)
        if content is None:
            return None
        for p in content.problems:
            if p.question_id == question_id:
                return await self._rt.repos.questions.get_question(question_id, p.concept_id)
        return None

    async def ensure_card(self, user: User, question: Question) -> Optional[ConceptState]:
        """The learner's card for a pattern_match question's pattern, created on
        their first review (lazily, BE-09 §2). None when they are not enrolled."""
        pm = question.pattern_match
        if pm is None or pm.track_id not in user.enrolled_track_ids():
            return None
        state = await self._rt.repos.concept_states.for_concept(user.id, question.concept_id)
        if state is not None:
            return state
        state = ConceptState(
            # Deterministic, so two first answers at once create one card.
            id=hashlib.sha256(f"card:{user.id}:{question.concept_id}".encode()).hexdigest()[:32],
            user_id=user.id,
            concept_id=question.concept_id,
            track_id=pm.track_id,
        )
        state, _ = await self._rt.repos.concept_states.create_if_absent(state, user.id)
        return await refresh_card_questions(self._rt, user.id, question.concept_id) or state

    # -- sessions ---------------------------------------------------------------
    async def draw(
        self,
        user: User,
        settings: UserSettings,
        *,
        track_id: Optional[str],
        budget_minutes: Optional[int],
        mix: Optional[dict[str, float]],
        limit: int,
        rng: Optional[random.Random] = None,
    ) -> DsaDraw:
        """A DSA session for `user` from their enrolled DSA tracks (or `track_id`)."""
        budget = min(budget_minutes or dsa.DEFAULT_BUDGET_MINUTES, dsa.MAX_BUDGET_MINUTES)
        enrolled = user.enrolled_track_ids()
        if track_id is not None:
            track_ids = [track_id] if track_id in enrolled else []
        else:
            track_ids = [
                t.id
                for t in await self._rt.repos.tracks.list_curated()
                if t.id in enrolled and t.kind == TrackKind.DSA_PATTERNS and t.archived_at is None
            ]
        now = _now()
        recent = {
            r.question_id
            for r in await self._rt.repos.reviews.created_since(
                user.id, now - timedelta(days=RECENT_DAYS)
            )
            if r.selected_index is not None
        }
        pool: list[dsa.Candidate] = []
        cards: dict[str, ConceptState] = {}
        for tid in track_ids:
            content = await self._rt.repos.track_content.get_content(tid)
            if content is None:
                continue
            for s in await self._rt.repos.concept_states.for_track(user.id, tid):
                cards[s.concept_id] = s
            for p in content.problems:
                state = cards.get(p.concept_id)
                pool.append(
                    dsa.Candidate(
                        question_id=p.question_id,
                        concept_id=p.concept_id,
                        leetcode_id=p.leetcode_id,
                        difficulty=p.difficulty,
                        time_weight=p.time_weight,
                        near_duplicate_of=p.near_duplicate_of,
                        tier=self._tier(state, settings, now),
                        recent=p.question_id in recent,
                    )
                )
        drawn = dsa.draw_session(pool, budget, mix, limit=limit, rng=rng)
        items: list[DrawnItem] = []
        for c in drawn:
            question = await self._rt.repos.questions.get_question(c.question_id, c.concept_id)
            if question is None or question.status != QuestionStatus.ACTIVE:
                continue
            state = cards.get(c.concept_id) or ConceptState(
                user_id=user.id,
                concept_id=c.concept_id,
                track_id=question.pattern_match.track_id if question.pattern_match else None,
            )
            items.append(DrawnItem(state=state, question=question))
        return DsaDraw(
            items=items,
            budget_minutes=budget,
            time_weight_total=sum(
                i.question.pattern_match.time_weight for i in items if i.question.pattern_match
            ),
        )

    @staticmethod
    def _tier(state: Optional[ConceptState], settings: UserSettings, now: datetime) -> int:
        if state is None or (state.reps == 0 and state.last_review_at is None):
            return dsa.TIER_NEW
        if is_retired(state, settings.retire_mastered):
            return dsa.TIER_RETIRED
        if state.due_at is not None and state.due_at <= now:
            return dsa.TIER_DUE
        return dsa.TIER_LEARNING
