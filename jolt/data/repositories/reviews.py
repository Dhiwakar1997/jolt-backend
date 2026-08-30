"""Reviews, AnswerFeedback and Sessions repositories (LLD §6).

reviews         partition by /user_id
answer_feedback partition by /review_id
sessions        partition by /user_id
"""

from __future__ import annotations

from typing import Optional

from jolt.data.repositories.base import Repository
from jolt.domain.models import AnswerFeedback, Review, ReviewStatus, Session


class ReviewsRepository(Repository[Review]):
    container_name = "reviews"
    model = Review

    async def get_review(self, review_id: str, user_id: str) -> Optional[Review]:
        return await self.get(review_id, partition_value=user_id)

    async def list_pending_gradings(self, user_id: str, limit: int) -> list[Review]:
        return await self._query(
            "SELECT * FROM c WHERE c.status = @pending "
            "ORDER BY c.created_at ASC OFFSET 0 LIMIT @limit",
            [
                {"name": "@pending", "value": ReviewStatus.PENDING_GRADING.value},
                {"name": "@limit", "value": limit},
            ],
            partition_value=user_id,
        )

    async def history_for_concept(self, user_id: str, concept_id: str) -> list[Review]:
        """Full review history for a (user, concept) — the FSRS fold input (LLD §7.4)."""
        return await self._query(
            "SELECT * FROM c WHERE c.concept_id = @cid ORDER BY c.created_at ASC",
            [{"name": "@cid", "value": concept_id}],
            partition_value=user_id,
        )


class AnswerFeedbackRepository(Repository[AnswerFeedback]):
    container_name = "answer_feedback"
    model = AnswerFeedback

    async def for_review(self, review_id: str) -> list[AnswerFeedback]:
        return await self._query("SELECT * FROM c", [], partition_value=review_id)


class SessionsRepository(Repository[Session]):
    container_name = "sessions"
    model = Session

    async def list_for_user(self, user_id: str) -> list[Session]:
        return await self._query(
            "SELECT * FROM c ORDER BY c.created_at DESC", [], partition_value=user_id
        )
