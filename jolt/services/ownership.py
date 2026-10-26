"""Ownership checks for track-, concept- and question-keyed calls (BE-07, decision A16).

Concepts and questions are partitioned by track and by concept, not by user, so
a partition read alone proves nothing about who owns the row. These helpers are
the one place that answers "is this the caller's?", for REST routes and MCP
tools alike (LLD §5 scope rule; docs/GAPS.md issue 1):

* A **track** is *visible* when it is the caller's own (user partition) or a
  curated one (world-readable), and *owned* only when it is the caller's own.
  Writes need an owned track; reads accept a visible one.
* A **concept** is the caller's when they hold its card (an FSRS state in
  their own partition). That is also how the concept's track is found.
* A **question** is the caller's when its concept is.

Every failure is the same "not found" a missing id gives, so ids of other
users' rows cannot be probed.
"""

from __future__ import annotations

from typing import Optional

from jolt.domain.models import Concept, ConceptState, Question, Track
from jolt.runtime import Runtime


class NotOwned(Exception):
    """The id does not exist for this caller (a 404 / a tool error)."""


async def visible_track(rt: Runtime, user_id: str, track_id: str) -> Track:
    """The caller's own track, or a curated one."""
    track = await rt.repos.tracks.get_track(track_id, user_id)
    if track is None:
        track = await rt.repos.tracks.get_track(track_id, None)
    if track is None:
        raise NotOwned("track not found")
    return track


async def owned_track(rt: Runtime, user_id: str, track_id: str) -> Track:
    """The caller's own track (writes; curated tracks are read-only)."""
    track = await rt.repos.tracks.get_track(track_id, user_id)
    if track is None:
        raise NotOwned("track not found")
    return track


async def owned_card(rt: Runtime, user_id: str, concept_id: str) -> ConceptState:
    """The caller's card for a concept; a concept without one is not theirs."""
    state = await rt.repos.concept_states.for_concept(user_id, concept_id)
    if state is None or not state.track_id:
        raise NotOwned("concept not found")
    return state


async def writable_card(rt: Runtime, user_id: str, concept_id: str) -> ConceptState:
    """The caller's card, on a track they own: curated concepts are read-only
    (BE-09), so writing questions or text for one is refused like a missing id."""
    state = await owned_card(rt, user_id, concept_id)
    if await rt.repos.tracks.get_track(state.track_id, user_id) is None:
        raise NotOwned("concept not found")
    return state


async def owned_concept(
    rt: Runtime, user_id: str, concept_id: str, *, track_id: Optional[str] = None
) -> tuple[Concept, ConceptState]:
    """The caller's concept and card. With `track_id`, it must also be on that track."""
    state = await owned_card(rt, user_id, concept_id)
    if track_id is not None and state.track_id != track_id:
        raise NotOwned("concept not found")
    concept = await rt.repos.concepts.get_concept(concept_id, state.track_id)
    if concept is None:
        raise NotOwned("concept not found")
    return concept, state


async def owned_question(rt: Runtime, user_id: str, concept_id: str, question_id: str) -> Question:
    question = await rt.repos.questions.get_question(question_id, concept_id)
    if question is None:
        raise NotOwned("question not found")
    if await rt.repos.concept_states.for_concept(user_id, concept_id) is None:
        raise NotOwned("question not found")
    return question


async def visible_concepts(
    rt: Runtime, user_id: str, track_id: str, *, active_only: bool = True
) -> tuple[list[Concept], dict[str, ConceptState]]:
    """The caller's concepts on a visible track, with their cards by concept id.

    Two single-partition queries (the track's concepts, the caller's cards on
    it) joined in memory. Concepts without the caller's card are left out:
    on a curated track those are other learners' (or Jolt's) rows.
    """
    await visible_track(rt, user_id, track_id)
    states = {s.concept_id: s for s in await rt.repos.concept_states.for_track(user_id, track_id)}
    concepts = await rt.repos.concepts.list_for_track(track_id, active_only=active_only)
    return [c for c in concepts if c.id in states], states
