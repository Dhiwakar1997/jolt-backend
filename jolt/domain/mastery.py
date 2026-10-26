"""Mastery and retirement rules (BE-06 §5).

A concept is **mastered** when, after a fold of its full review history, its
FSRS stability (in days) is at or above `MASTERY_STABILITY_DAYS` (21 by default)
and the latest grade was Good or Easy. Stability is FSRS's estimate of how many
days until recall drops to 90 %, so 21 means "still remembered three weeks
out". A lapse (or a final grade that lowers stability) un-masters it.

A mastered concept is **retired** (leaves the review queue) only while the
user's `retire_mastered` setting is on, and not if the user unretired it after
the review that last confirmed mastery. Retirement is therefore evaluated at
read time: turning the setting off brings every mastered concept back at once,
turning it on retires them at once, with no rewrite of the cards.

Pure functions: no I/O, no framework imports.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional

from jolt.domain.models import ConceptState, FSRSGrade, Review


def last_graded(reviews: Iterable[Review]) -> tuple[Optional[int], Optional[datetime]]:
    """(grade, reviewed_at) of the latest review the FSRS fold applied."""
    grade: Optional[int] = None
    at: Optional[datetime] = None
    for review in reviews:
        value = review.final_fsrs_grade or review.provisional_fsrs_grade
        if value is None:
            continue
        grade, at = value, review.free_text_locked_at or review.created_at
    return grade, at


def is_mastered(stability: float, last_grade: Optional[int], threshold_days: float) -> bool:
    return (
        last_grade is not None
        and last_grade >= FSRSGrade.GOOD.value
        and stability >= threshold_days
    )


def mastered_at(
    state: ConceptState, reviews: list[Review], threshold_days: float
) -> Optional[datetime]:
    """The `mastered_at` a freshly folded `state` should carry."""
    grade, at = last_graded(reviews)
    return at if is_mastered(state.stability, grade, threshold_days) else None


def is_retired(state: ConceptState, retire_mastered: bool) -> bool:
    """Whether the concept is out of the queue right now."""
    if not retire_mastered or state.mastered_at is None:
        return False
    return state.unretired_at is None or state.unretired_at < state.mastered_at
