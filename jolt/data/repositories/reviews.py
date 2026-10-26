"""Reviews, AnswerFeedback and Sessions repositories (LLD §6).

reviews         partition by /user_id
answer_feedback partition by /review_id
sessions        partition by /user_id
review_sessions partition by /user_id (BE-06)
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from jolt.data.repositories.base import Repository
from jolt.domain.models import AnswerFeedback, Review, ReviewSession, ReviewStatus, Session


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

    async def for_session(self, user_id: str, session_id: str) -> list[Review]:
        """Reviews answered in one review session (BE-06), oldest first."""
        return await self._query(
            "SELECT * FROM c WHERE c.session_id = @sid ORDER BY c.created_at ASC",
            [{"name": "@sid", "value": session_id}],
            partition_value=user_id,
        )

    async def created_since(self, user_id: str, since: datetime) -> list[Review]:
        """Reviews started at or after `since` ("done today")."""
        return await self._query(
            "SELECT * FROM c WHERE c.created_at >= @since",
            [{"name": "@since", "value": since.isoformat()}],
            partition_value=user_id,
        )


class ReviewSessionsRepository(Repository[ReviewSession]):
    container_name = "review_sessions"
    model = ReviewSession

    async def get_session(self, session_id: str, user_id: str) -> Optional[ReviewSession]:
        return await self.get(session_id, partition_value=user_id)


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

    async def for_sync_run(self, user_id: str, sync_run_id: str) -> list[Session]:
        """Sessions logged during one sync run (one per upload date, D6)."""
        return await self._query(
            "SELECT * FROM c WHERE c.sync_run_id = @run ORDER BY c.created_at ASC",
            [{"name": "@run", "value": sync_run_id}],
            partition_value=user_id,
        )
