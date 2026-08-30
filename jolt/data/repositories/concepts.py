"""Concepts, ConceptStates and Questions repositories (LLD §6).

concepts     partition by /track_id  (concepts of a track read together)
concept_states partition by /user_id  (a user's whole memory state — the hot path)
questions    partition by /concept_id (questions for a concept read together)
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from jolt.data.repositories.base import Repository
from jolt.domain.models import (
    Concept,
    ConceptState,
    ConceptStatus,
    Question,
    QuestionStatus,
)


class ConceptsRepository(Repository[Concept]):
    container_name = "concepts"
    model = Concept

    async def get_concept(self, concept_id: str, track_id: str) -> Optional[Concept]:
        return await self.get(concept_id, partition_value=track_id)

    async def list_for_track(self, track_id: str, *, active_only: bool = True) -> list[Concept]:
        if active_only:
            return await self._query(
                "SELECT * FROM c WHERE c.status = @active",
                [{"name": "@active", "value": ConceptStatus.ACTIVE.value}],
                partition_value=track_id,
            )
        return await self._query("SELECT * FROM c", [], partition_value=track_id)


class ConceptStatesRepository(Repository[ConceptState]):
    container_name = "concept_states"
    model = ConceptState

    async def get_state(self, state_id: str, user_id: str) -> Optional[ConceptState]:
        return await self.get(state_id, partition_value=user_id)

    async def for_concept(self, user_id: str, concept_id: str) -> Optional[ConceptState]:
        rows = await self._query(
            "SELECT * FROM c WHERE c.concept_id = @cid",
            [{"name": "@cid", "value": concept_id}],
            partition_value=user_id,
        )
        return rows[0] if rows else None

    async def due(self, user_id: str, now: datetime, limit: int = 100) -> list[ConceptState]:
        """Everything due for this user — single-partition on user_id (LLD §7.5).

        Backed by the composite index on (user_id, due_at).
        """
        return await self._query(
            "SELECT * FROM c WHERE c.due_at <= @now ORDER BY c.due_at ASC "
            "OFFSET 0 LIMIT @limit",
            [
                {"name": "@now", "value": now.isoformat()},
                {"name": "@limit", "value": limit},
            ],
            partition_value=user_id,
        )

    async def all_for_user(self, user_id: str) -> list[ConceptState]:
        return await self._query("SELECT * FROM c", [], partition_value=user_id)


class QuestionsRepository(Repository[Question]):
    container_name = "questions"
    model = Question

    async def get_question(self, question_id: str, concept_id: str) -> Optional[Question]:
        return await self.get(question_id, partition_value=concept_id)

    async def list_for_concept(self, concept_id: str, *, active_only: bool = True) -> list[Question]:
        if active_only:
            return await self._query(
                "SELECT * FROM c WHERE c.status = @active",
                [{"name": "@active", "value": QuestionStatus.ACTIVE.value}],
                partition_value=concept_id,
            )
        return await self._query("SELECT * FROM c", [], partition_value=concept_id)
