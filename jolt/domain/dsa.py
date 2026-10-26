"""DSA pattern trainer rules (BE-09): the session draw and the match verdict.

Ported from v1 (`src/tracks/dsa/lib/session.ts`, `scoring.ts`), with spaced
repetition deciding which patterns come first.

**Draw** (v1 `buildSession`):

* ONE continuous clock for the whole session: problems are drawn until their
  cumulative `time_weight` (Easy 2, Medium 4, Hard 7 minutes) fills the budget,
  so a hard sitting holds fewer problems than an easy one.
* Each pick first orders the difficulties by a weighted random shuffle of the
  difficulty mix (v1 default 20 / 55 / 25), then takes the best candidate of the
  first difficulty that still has one that fits the remaining budget.
* Never two near duplicates in one session: a scheduled problem blocks its
  `near_duplicate_of` partner, either way round.

**Which candidate is best** (the 2.0 part): a problem whose pattern is due
comes before one whose pattern is new to the learner, which comes before one
whose pattern is still learning but not due; a pattern already in this session
goes behind all of those, and a pattern the learner retired (mastered) last.
Ties go to a problem the learner has not answered recently, then at random.

**Verdict** (v1 `scoreAnswer`): the problem's `algorithm` is `correct`, one of
its `acceptableAlgorithms` is `partial`, anything else `incorrect`. It is final
at once (no semantic grading): FSRS Good, Hard and Again respectively.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from jolt.domain.models import FSRSGrade

DIFFICULTIES = ("Easy", "Medium", "Hard")
DEFAULT_MIX = {"Easy": 20, "Medium": 55, "Hard": 25}
DEFAULT_BUDGET_MINUTES = 30
MAX_BUDGET_MINUTES = 240

# Pattern priority tiers (lower draws first).
TIER_DUE = 0
TIER_NEW = 1
TIER_LEARNING = 2
TIER_RETIRED = 4
_SAME_PATTERN_PENALTY = 3  # a pattern already drawn this session sinks below learning

MATCH_GRADE = {
    "correct": FSRSGrade.GOOD,
    "partial": FSRSGrade.HARD,
    "incorrect": FSRSGrade.AGAIN,
}


@dataclass(frozen=True)
class Candidate:
    question_id: str
    concept_id: str
    leetcode_id: int
    difficulty: str
    time_weight: int
    near_duplicate_of: Optional[int] = None
    tier: int = TIER_NEW
    recent: bool = False  # answered by the learner recently


def match_verdict(selected_index: int, correct_index: int, acceptable: Iterable[int]) -> str:
    if selected_index == correct_index:
        return "correct"
    if selected_index in set(acceptable):
        return "partial"
    return "incorrect"


def match_feedback(verdict: str, picked: str, answer: str) -> str:
    """The coaching line v1 showed after a match (correct / defensible / mismatch)."""
    if verdict == "correct":
        return f"Correct: this is a {answer} problem."
    if verdict == "partial":
        return f"Defensible: {picked} can work here, but the intended pattern is {answer}."
    return f"Not this time: you picked {picked}; this is a {answer} problem."


def normalise_mix(mix: Optional[dict[str, float]]) -> dict[str, float]:
    mix = mix or DEFAULT_MIX
    weights = {d: max(0.0, float(mix.get(d, 0) or 0)) for d in DIFFICULTIES}
    total = sum(weights.values())
    if total <= 0:
        return {d: 1 / 3 for d in DIFFICULTIES}
    return {d: w / total for d, w in weights.items()}


def weighted_difficulty_order(weights: dict[str, float], rng: random.Random) -> list[str]:
    """v1 `weightedDifficultyOrder`: a weighted random shuffle of the difficulties."""
    remaining = list(DIFFICULTIES)
    out: list[str] = []
    while remaining:
        total = sum(weights[d] for d in remaining)
        r = rng.random() * (total or 1)
        picked = remaining[0]
        for d in remaining:
            r -= weights[d] or 0.0001
            if r <= 0:
                picked = d
                break
        out.append(picked)
        remaining.remove(picked)
    return out


def draw_session(
    pool: Sequence[Candidate],
    budget_minutes: int,
    mix: Optional[dict[str, float]] = None,
    *,
    limit: Optional[int] = None,
    rng: Optional[random.Random] = None,
) -> list[Candidate]:
    """Draw a DSA session queue without replacement (see the module docstring)."""
    rng = rng or random.Random()
    weights = normalise_mix(mix)
    # A random rank per problem breaks ties, so sessions feel fresh each time.
    rank = {c.question_id: rng.random() for c in pool}
    buckets: dict[str, list[Candidate]] = {d: [] for d in DIFFICULTIES}
    for c in pool:
        if c.difficulty in buckets:
            buckets[c.difficulty].append(c)

    queue: list[Candidate] = []
    used: set[str] = set()
    blocked: set[int] = set()  # leetcode ids a scheduled problem rules out
    patterns: set[str] = set()
    spent = 0

    def key(c: Candidate) -> tuple:
        tier = c.tier + (_SAME_PATTERN_PENALTY if c.concept_id in patterns else 0)
        return (tier, c.recent, rank[c.question_id])

    while spent < budget_minutes and (limit is None or len(queue) < limit):
        drew = False
        for diff in weighted_difficulty_order(weights, rng):
            fits = [
                c
                for c in buckets[diff]
                if c.question_id not in used
                and c.leetcode_id not in blocked
                and (c.near_duplicate_of is None or c.near_duplicate_of not in blocked)
                and spent + c.time_weight <= budget_minutes
            ]
            if not fits:
                continue
            best = min(fits, key=key)
            queue.append(best)
            used.add(best.question_id)
            blocked.add(best.leetcode_id)
            if best.near_duplicate_of is not None:
                blocked.add(best.near_duplicate_of)
            patterns.add(best.concept_id)
            spent += best.time_weight
            drew = True
            break
        if not drew:
            break  # nothing left fits the remaining budget
    return queue
