"""Syllabus ↔ concept coverage computation (LLD §4 coverage, §6 query discipline).

`get_coverage` reads a track's concepts (single partition on track_id) plus the
user's concept_states, and joins them in memory — no cross-partition query. This
module is that in-memory join, kept pure so it needs neither Cosmos nor a track
loaded from the network to test.

**Off-plan concepts** (BE-07 §5). Auto-add keeps an unlocked agenda in step
with what is learned, so a concept outside the syllabus only appears on a
*locked* track (auto-add is skipped there). Such a concept is kept, reviewed as
usual and listed on Track detail flagged `off_plan`, but it is outside the plan,
so it does not count towards coverage: it is left out of every total here and
reported as `off_plan_concepts`.

"Decay" is the share of a track's concepts whose retrievability has fallen below a
threshold — a cheap health signal for the app's progress view.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from jolt.domain.models import AgendaStatus, Concept, ConceptState, Track

_DECAY_RETRIEVABILITY = 0.7  # below this a concept is "decaying"


@dataclass
class SyllabusCoverage:
    syllabus_ref: str
    total_concepts: int = 0
    studied_concepts: int = 0  # have a concept_state

    @property
    def coverage_ratio(self) -> float:
        if self.total_concepts == 0:
            return 0.0
        return self.studied_concepts / self.total_concepts


@dataclass
class CoverageReport:
    track_id: str
    total_concepts: int = 0
    studied_concepts: int = 0
    decaying_concepts: int = 0
    off_plan_concepts: int = 0
    by_syllabus: list[SyllabusCoverage] = field(default_factory=list)
    uncovered_syllabus: list[str] = field(default_factory=list)

    @property
    def coverage_ratio(self) -> float:
        if self.total_concepts == 0:
            return 0.0
        return self.studied_concepts / self.total_concepts


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_off_plan(track: Track, concept: Concept) -> bool:
    """A concept on a locked track whose key is not in the agenda (BE-07 §5)."""
    return track.agenda.status == AgendaStatus.LOCKED and (
        concept.syllabus_ref not in track.agenda.keys()
    )


def compute_coverage(
    track: Track,
    concepts: list[Concept],
    states: list[ConceptState],
    *,
    now: datetime | None = None,
) -> CoverageReport:
    """Join a track's concepts with the user's states into a coverage report."""
    now = now or _now()
    state_by_concept = {s.concept_id: s for s in states}

    on_plan = [c for c in concepts if not is_off_plan(track, c)]
    report = CoverageReport(
        track_id=track.id,
        total_concepts=len(on_plan),
        off_plan_concepts=len(concepts) - len(on_plan),
    )

    # Coverage maps concepts against the agenda's syllabus, keyed on concept_key.
    keys = [item.concept_key for item in track.agenda.syllabus]
    syllabus_buckets: dict[str, SyllabusCoverage] = {
        key: SyllabusCoverage(syllabus_ref=key) for key in keys
    }
    covered_refs: set[str] = set()

    for concept in on_plan:
        state = state_by_concept.get(concept.id)
        studied = state is not None
        if studied:
            report.studied_concepts += 1
            if _is_decaying(state, now):
                report.decaying_concepts += 1

        ref = concept.syllabus_ref
        if ref and ref in syllabus_buckets:
            bucket = syllabus_buckets[ref]
            bucket.total_concepts += 1
            if studied:
                bucket.studied_concepts += 1
                covered_refs.add(ref)

    report.by_syllabus = list(syllabus_buckets.values())
    report.uncovered_syllabus = [key for key in keys if key not in covered_refs]
    return report


def _is_decaying(state: ConceptState, now: datetime) -> bool:
    if state.retrievability and state.retrievability < _DECAY_RETRIEVABILITY:
        return True
    # A state past its due date with no fresh retrievability estimate is decaying.
    return state.due_at is not None and state.due_at < now
