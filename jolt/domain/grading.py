"""Grading domain rules (LLD §4 grading, §7.2, §7.3).

Two grade sources meet here:
  * provisional — synthesised at stage-2 submit from correctness + effort (§7.3).
  * final/semantic — supplied by an agent via `submit_gradings` during sync (§7.2),
    graded against the expected answer and the versioned rubric.

Grade submission is idempotent on `review_id` (§8): an agent retry after a dropped
connection must not double-write, so applying a grading to an already-graded review
is a no-op that returns the existing state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from jolt.domain.models import (
    AnswerFeedback,
    FSRSGrade,
    Question,
    Review,
    ReviewStatus,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class StageTwoOutcome:
    review: Review
    is_correct: bool
    expected_answer: str
    correct_index: int


def score_stage_two(
    review: Review,
    question: Question,
    selected_index: int,
    latency_ms: Optional[int],
) -> StageTwoOutcome:
    """Compute correctness and the provisional grade for a stage-2 submission."""
    from jolt.domain.scheduling import synthesise_provisional_grade

    is_correct = selected_index == question.correct_index
    stage1_len = len(review.free_text or "")
    provisional = synthesise_provisional_grade(is_correct, stage1_len, latency_ms)

    review.selected_index = selected_index
    review.is_correct = is_correct
    review.latency_ms = latency_ms
    review.provisional_fsrs_grade = provisional.value
    # Stays pending-grading until the semantic final grade arrives via sync.
    return StageTwoOutcome(
        review=review,
        is_correct=is_correct,
        expected_answer=question.expected_answer,
        correct_index=question.correct_index,
    )


@dataclass
class GradingInput:
    """One item of an agent's `submit_gradings` batch (§7.2)."""

    review_id: str
    suggested_fsrs_grade: int
    rationale: Optional[str] = None
    rubric_version: Optional[str] = None


@dataclass
class GradingResult:
    review: Review
    feedback: Optional[AnswerFeedback]
    applied: bool  # False when this was an idempotent no-op


def apply_grading(review: Review, grading: GradingInput) -> GradingResult:
    """Apply a semantic grading to a review, idempotently (LLD §8).

    Idempotency key is the review's graded state: once a review carries a final
    grade, re-applying returns the existing review with `applied=False` and no new
    feedback document.
    """
    if review.status == ReviewStatus.GRADED and review.final_fsrs_grade is not None:
        return GradingResult(review=review, feedback=None, applied=False)

    grade = FSRSGrade(grading.suggested_fsrs_grade)  # validates the range
    review.final_fsrs_grade = grade.value
    review.status = ReviewStatus.GRADED

    feedback = AnswerFeedback(
        review_id=review.id,
        user_id=review.user_id,
        suggested_fsrs_grade=grade.value,
        rationale=grading.rationale,
        rubric_version=grading.rubric_version,
    )
    return GradingResult(review=review, feedback=feedback, applied=True)
