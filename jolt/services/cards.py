"""Card denormalisation for the read models (BE-07).

Track detail shows, per concept, its question count, current question version
and the question the next session serves. Questions are partitioned by concept,
so reading them per concept would be one query per row (N+1). Instead the
caller's card (FSRS state, user partition) carries `active_question_ids` (the
rotation order the queue uses) and `question_version`, refreshed here whenever
the concept's active questions change: jolt_create_questions, a graded rewrite,
and supersede reconciliation.
"""

from __future__ import annotations

from typing import Optional

from jolt.domain.models import ConceptState, Question
from jolt.runtime import Runtime


def rotation(active: list[Question]) -> list[Question]:
    """Active questions in the order the review queue rotates through them."""
    return sorted(active, key=lambda q: (q.created_at, q.id))


async def refresh_card_questions(
    rt: Runtime, user_id: str, concept_id: str
) -> Optional[ConceptState]:
    """Re-read the concept's active questions and store them on the user's card."""
    active = rotation(await rt.repos.questions.list_for_concept(concept_id))
    ids = [q.id for q in active]
    version = max((q.version for q in active), default=None)
    for _ in range(5):
        state = await rt.repos.concept_states.for_concept(user_id, concept_id)
        if state is None:
            return None
        if state.active_question_ids == ids and state.question_version == version:
            return state
        state.active_question_ids = ids
        state.question_version = version
        saved = await rt.repos.concept_states.replace(state, etag=state.etag)
        if saved is not None:
            return saved
    return None


def current_question_id(state: ConceptState) -> Optional[str]:
    """The question the next session serves for this card (same rule as the queue)."""
    ids = state.active_question_ids or []
    return ids[state.reps % len(ids)] if ids else None
