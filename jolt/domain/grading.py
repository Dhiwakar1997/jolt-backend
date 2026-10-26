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


def score_pattern_match(
    review: Review,
    question: Question,
    selected_index: int,
    latency_ms: Optional[int],
) -> str:
    """Score a `pattern_match` answer (BE-09): deterministic and final at once.

    correct (the problem's pattern) → Good, partial (an acceptable pattern) →
    Hard, incorrect → Again. No semantic grading pass follows: the review is
    `graded` immediately, with v1's coaching line as its feedback.
    """
    from jolt.domain.dsa import MATCH_GRADE, match_feedback, match_verdict

    verdict = match_verdict(selected_index, question.correct_index, question.acceptable_indexes)
    grade = MATCH_GRADE[verdict]
    picked = question.options[selected_index] if 0 <= selected_index < len(question.options) else ""
    review.selected_index = selected_index
    review.is_correct = verdict == "correct"
    review.latency_ms = latency_ms
    review.match = verdict
    review.provisional_fsrs_grade = grade.value
    review.final_fsrs_grade = grade.value
    review.status = ReviewStatus.GRADED
    review.feedback = match_feedback(verdict, picked, question.expected_answer)
    return verdict


@dataclass
class QuestionRewrite:
    """A rewritten question proposed with a grading (decision D8, BE-06)."""

    stem: str
    options: list[str]
    correct_index: int
    expected_answer: str
    rewrite_reason: Optional[str] = None

    def problem(self) -> Optional[str]:
        """Why this rewrite cannot be stored, or None when it is valid."""
        if not isinstance(self.stem, str) or not self.stem.strip():
            return "stem is empty"
        if not isinstance(self.options, list) or len(self.options) < 2:
            return "needs at least two options"
        if not all(isinstance(o, str) and o.strip() for o in self.options):
            return "options must be non-empty strings"
        if not isinstance(self.correct_index, int) or not (
            0 <= self.correct_index < len(self.options)
        ):
            return "correct_index is not an index into options"
        if not isinstance(self.expected_answer, str) or not self.expected_answer.strip():
            return "expected_answer is empty"
        return None


@dataclass
class GradingInput:
    """One item of an agent's `submit_gradings` batch (§7.2)."""

    review_id: str
    suggested_fsrs_grade: int
    rationale: Optional[str] = None
    rubric_version: Optional[str] = None
    # BE-06: an optional next version of the reviewed question.
    rewritten_question: Optional[QuestionRewrite] = None


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
    # The rationale doubles as the feedback summary the learner sees (BE-06 §3).
    review.feedback = grading.rationale

    feedback = AnswerFeedback(
        review_id=review.id,
        user_id=review.user_id,
        suggested_fsrs_grade=grade.value,
        rationale=grading.rationale,
        rubric_version=grading.rubric_version,
    )
    return GradingResult(review=review, feedback=feedback, applied=True)
