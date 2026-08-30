"""Two-stage review flow with inline FSRS recompute (LLD §7.3, §7.5).

Stage 2 folds FSRS over the card's full history — including the grade just
produced — and writes the updated card fields in the same request. No job, no
deferral (per the single-service course correction).

Server-side stage lock (§7.3): stage-2 options are not returned until stage 1 is
submitted, so a modified client cannot peek at the choices.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from jolt.domain.grading import score_stage_two
from jolt.domain.models import ConceptState, Question, Review, ReviewStatus
from jolt.domain.scheduling import fold_history
from jolt.runtime import Runtime


class ReviewError(Exception):
    pass


@dataclass
class DueItem:
    concept_id: str
    question_id: str
    stem: str


@dataclass
class StageOneResult:
    review_id: str
    options: list[str]  # released only now


@dataclass
class StageTwoResult:
    correct: bool
    expected_answer: str
    correct_index: int
    due_at: Optional[datetime]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ReviewService:
    def __init__(self, rt: Runtime) -> None:
        self._rt = rt

    async def due(self, user_id: str, limit: int = 100) -> list[DueItem]:
        """Due questions — single-partition query on concept_states (LLD §7.5)."""
        states = await self._rt.repos.concept_states.due(user_id, _now(), limit=limit)
        items: list[DueItem] = []
        for state in states:
            questions = await self._rt.repos.questions.list_for_concept(state.concept_id)
            for q in questions:
                items.append(DueItem(concept_id=q.concept_id, question_id=q.id, stem=q.stem))
        return items

    async def submit_stage_one(
        self, user_id: str, concept_id: str, question_id: str, free_text: str
    ) -> StageOneResult:
        question = await self._rt.repos.questions.get_question(question_id, concept_id)
        if question is None:
            raise ReviewError("question not found")

        review = Review(
            user_id=user_id,
            question_id=question_id,
            concept_id=concept_id,
            free_text=free_text,
            free_text_locked_at=_now(),
            status=ReviewStatus.PENDING_GRADING,
        )
        created = await self._rt.repos.reviews.create(review)
        # Options released only now that stage 1 is locked.
        return StageOneResult(review_id=created.id, options=list(question.options))

    async def submit_stage_two(
        self, user_id: str, review_id: str, selected_index: int, latency_ms: int | None
    ) -> StageTwoResult:
        review = await self._rt.repos.reviews.get_review(review_id, user_id)
        if review is None:
            raise ReviewError("review not found")
        if review.free_text_locked_at is None:
            raise ReviewError("stage 1 not submitted for this review")
        if review.selected_index is not None:
            raise ReviewError("stage 2 already submitted")

        question = await self._rt.repos.questions.get_question(
            review.question_id, review.concept_id
        )
        if question is None:
            raise ReviewError("question not found")

        outcome = score_stage_two(review, question, selected_index, latency_ms)
        review = await self._rt.repos.reviews.replace(review, etag=review.etag) or review

        state = await self._recompute_card(user_id, review.concept_id)

        return StageTwoResult(
            correct=outcome.is_correct,
            expected_answer=outcome.expected_answer,
            correct_index=outcome.correct_index,
            due_at=state.due_at,
        )

    async def _recompute_card(self, user_id: str, concept_id: str) -> ConceptState:
        """Fold FSRS over the card's full history and persist — inline (LLD §7.4)."""
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
