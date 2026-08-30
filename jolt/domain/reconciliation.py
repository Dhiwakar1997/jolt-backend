"""Re-extraction reconciliation — supersede / diff / classify (LLD §7.7, spec §8).

Pure classification: given the concepts derived from the currently-active
extraction and the concepts proposed by a new extraction, decide per concept
whether it is unchanged, refined, changed, removed, or new. The data-layer
orchestration (stamp history before moving pointers, carry FSRS state, retire
questions) lives in the write path that consumes this classification; keeping the
decision pure makes the dangerous path testable without Cosmos.

Matching is text-similarity based (no inference, per LLD §1): concepts are paired
by best normalised-text overlap, then the pair's edit distance decides refined vs
changed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum

from jolt.domain.models import Concept


class ChangeClass(str, Enum):
    UNCHANGED = "unchanged"
    REFINED = "refined"
    CHANGED = "changed"
    REMOVED = "removed"
    NEW = "new"


# Similarity thresholds (0..1). Above MATCH we consider two concepts "the same
# concept"; among matched pairs, above REFINE_CEILING is unchanged, and the band
# between is a refinement rather than a semantic change.
_MATCH_THRESHOLD = 0.55
_UNCHANGED_THRESHOLD = 0.995
_REFINE_THRESHOLD = 0.80


@dataclass
class ProposedConcept:
    """A concept the new extraction proposes. Carries no id yet."""

    text: str
    title: str | None = None
    syllabus_ref: str | None = None


@dataclass
class ConceptDelta:
    change: ChangeClass
    existing: Concept | None = None  # None for NEW
    proposed: ProposedConcept | None = None  # None for REMOVED
    similarity: float = 0.0


@dataclass
class Reconciliation:
    deltas: list[ConceptDelta] = field(default_factory=list)

    def of(self, change: ChangeClass) -> list[ConceptDelta]:
        return [d for d in self.deltas if d.change == change]


def _normalise(text: str) -> str:
    return " ".join(text.lower().split())


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalise(a), _normalise(b)).ratio()


def reconcile(
    existing: list[Concept],
    proposed: list[ProposedConcept],
) -> Reconciliation:
    """Classify each concept. Greedy best-match pairing (LLD §7.7)."""
    remaining_existing = list(existing)
    deltas: list[ConceptDelta] = []

    for prop in proposed:
        best: Concept | None = None
        best_sim = 0.0
        for cand in remaining_existing:
            sim = _similarity(prop.text, cand.text)
            if sim > best_sim:
                best_sim, best = sim, cand

        if best is not None and best_sim >= _MATCH_THRESHOLD:
            remaining_existing.remove(best)
            if best_sim >= _UNCHANGED_THRESHOLD:
                change = ChangeClass.UNCHANGED
            elif best_sim >= _REFINE_THRESHOLD:
                change = ChangeClass.REFINED
            else:
                change = ChangeClass.CHANGED
            deltas.append(
                ConceptDelta(change=change, existing=best, proposed=prop, similarity=best_sim)
            )
        else:
            deltas.append(
                ConceptDelta(change=ChangeClass.NEW, proposed=prop, similarity=best_sim)
            )

    # Anything left unmatched in the active extraction was removed.
    for leftover in remaining_existing:
        deltas.append(ConceptDelta(change=ChangeClass.REMOVED, existing=leftover))

    return Reconciliation(deltas=deltas)
