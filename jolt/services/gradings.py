"""Semantic grade submission and question versions (LLD §7.2, BE-06 §4, decision D8).

Backs `jolt_submit_gradings`. Each grading is applied idempotently per review
(`jolt.domain.grading.apply_grading`), the affected cards are re-folded inline
(with mastery), and an optional `rewritten_question` becomes the next version
of the reviewed question — only when the user turned on `allow_question_rewrite`.

Versions: version n+1 is a new `Question` in the same concept partition with
`previous_version_id` = version n; version n is marked `retired` with
`superseded_by` = n+1 and stays stored (the concept's timeline shows it). The
new version's id is derived from version n's id, so two rewrites of the same
version race on one id: exactly one is created, and the loser builds on the
winner (n+2). A retried submission finds its own version and does not fork.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from jolt.domain.grading import GradingInput, QuestionRewrite, apply_grading
from jolt.domain.models import Question, QuestionStatus, Review
from jolt.runtime import Runtime
from jolt.services.reviews import ReviewService

REWRITE_OFF = "question rewriting is off for this user (allow_question_rewrite)"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def next_version_id(question_id: str) -> str:
    """The id version n+1 gets when version `question_id` is rewritten."""
    return hashlib.sha256(f"next-version:{question_id}".encode()).hexdigest()[:32]


@dataclass
class Rewritten:
    review_id: str
    question_id: str
    version: int
    previous_version_id: Optional[str]
    concept_id: str = ""
    # False when a retried grading only echoes the version its first try made.
    created: bool = True


@dataclass
class RewriteSkipped:
    review_id: str
    reason: str


@dataclass
class GradingOutcome:
    applied: int
    submitted: int
    rewritten: list[Rewritten] = field(default_factory=list)
    rewrites_skipped: list[RewriteSkipped] = field(default_factory=list)


class GradingService:
    def __init__(self, rt: Runtime) -> None:
        self._rt = rt
        self._reviews = ReviewService(rt)

    async def submit(self, user_id: str, gradings: list[GradingInput]) -> GradingOutcome:
        """Apply semantic grades idempotently, store rewrites, re-fold each card inline."""
        settings = await self._reviews.user_settings(user_id)
        outcome = GradingOutcome(applied=0, submitted=len(gradings))
        touched: set[str] = set()
        for g in gradings:
            review = await self._rt.repos.reviews.get_review(g.review_id, user_id)
            if review is None:
                if g.rewritten_question is not None:
                    outcome.rewrites_skipped.append(RewriteSkipped(g.review_id, "review not found"))
                continue
            result = apply_grading(review, g)
            if not result.applied:
                if g.rewritten_question is not None:
                    await self._report_existing(review, outcome)
                continue

            if g.rewritten_question is not None:
                await self._rewrite(review, g.rewritten_question, settings, outcome)

            await self._rt.repos.reviews.replace(result.review, etag=result.review.etag)
            if result.feedback is not None:
                await self._rt.repos.answer_feedback.create(result.feedback)
            outcome.applied += 1
            touched.add(review.concept_id)

        # Inline FSRS re-fold for each concept whose grade changed (LLD §7.4).
        for concept_id in touched:
            await self._reviews.recompute_card(user_id, concept_id)
        return outcome

    async def _report_existing(self, review: Review, outcome: GradingOutcome) -> None:
        """A retried grading: echo the version its first submission created."""
        if review.rewritten_question_id:
            q = await self._rt.repos.questions.get_question(
                review.rewritten_question_id, review.concept_id
            )
            if q is not None:
                outcome.rewritten.append(
                    Rewritten(
                        review.id,
                        q.id,
                        q.version,
                        q.previous_version_id,
                        concept_id=review.concept_id,
                        created=False,
                    )
                )
                return
        outcome.rewrites_skipped.append(RewriteSkipped(review.id, "review already graded"))

    async def _rewrite(
        self, review: Review, rewrite: QuestionRewrite, settings, outcome: GradingOutcome
    ) -> None:
        if not settings.allow_question_rewrite:
            outcome.rewrites_skipped.append(RewriteSkipped(review.id, REWRITE_OFF))
            return
        problem = rewrite.problem()
        if problem is not None:
            outcome.rewrites_skipped.append(
                RewriteSkipped(review.id, f"invalid rewritten_question: {problem}")
            )
            return
        created = await self.create_version(review, rewrite)
        if created is None:
            outcome.rewrites_skipped.append(RewriteSkipped(review.id, "question not found"))
            return
        review.rewritten_question_id = created.id
        outcome.rewritten.append(
            Rewritten(
                review.id,
                created.id,
                created.version,
                created.previous_version_id,
                concept_id=review.concept_id,
            )
        )

    async def create_version(self, review: Review, rewrite: QuestionRewrite) -> Optional[Question]:
        """Create the next version of the reviewed question's chain; retire the old head."""
        questions = self._rt.repos.questions
        head = await questions.get_question(review.question_id, review.concept_id)
        if head is None:
            return None
        for _ in range(5):
            head, own = await self._head(head, review.id)
            if own is not None:  # a retry after this review's version was created
                return own
            draft = Question(
                id=next_version_id(head.id),
                concept_id=head.concept_id,
                stem=rewrite.stem.strip(),
                options=list(rewrite.options),
                correct_index=rewrite.correct_index,
                expected_answer=rewrite.expected_answer.strip(),
                version=head.version + 1,
                previous_version_id=head.id,
                rewrite_reason=(rewrite.rewrite_reason or "").strip() or None,
                rewritten_from_review_id=review.id,
            )
            stored, created = await questions.create_if_absent(
                draft, partition_value=head.concept_id
            )
            if created or stored.rewritten_from_review_id == review.id:
                # New, or our own earlier attempt: make sure the old head is retired.
                await self._retire(head, stored.id)
                return stored
            head = stored  # another grading rewrote this version first: build on it
        return None

    async def _head(
        self, question: Question, review_id: str
    ) -> tuple[Question, Optional[Question]]:
        """Follow `superseded_by` to the newest version.

        Returns (head, own): `own` is a version on the way that this review's
        rewrite already created (a retried submission), else None.
        """
        for _ in range(100):
            if question.rewritten_from_review_id == review_id:
                return question, question
            if not question.superseded_by:
                break
            nxt = await self._rt.repos.questions.get_question(
                question.superseded_by, question.concept_id
            )
            if nxt is None:
                break
            question = nxt
        return question, None

    async def _retire(self, question: Question, successor_id: str) -> None:
        current: Optional[Question] = question
        for _ in range(5):
            if current is None:
                return
            if current.status == QuestionStatus.RETIRED and current.superseded_by == successor_id:
                return
            current.status = QuestionStatus.RETIRED
            current.superseded_by = successor_id
            current.retired_at = _now()
            if await self._rt.repos.questions.replace(current, etag=current.etag) is not None:
                return
            current = await self._rt.repos.questions.get_question(current.id, current.concept_id)
