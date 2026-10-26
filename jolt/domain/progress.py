"""Progress arithmetic (BE-08): predicted recall, learner days, streaks.

Pure functions, no I/O. `jolt.services.progress` gathers the documents and
calls these.

**Predicted recall** is the FSRS forgetting curve the scheduler itself uses
(FSRS-4.5 / py-fsrs 4+): `R(t) = (1 + FACTOR * t / S) ** DECAY` with `t` the
days since the last review and `S` the card's stability in days, so `R = 0.9`
when `t = S`. Unlike `fsrs.Card.get_retrievability` (whole days), `t` here is
fractional, so the number moves smoothly through the day.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, tzinfo
from typing import Iterable, Optional

DECAY = -0.5
FACTOR = 0.9 ** (1 / DECAY) - 1  # 19/81: R(S) = 0.9
_DAY_SECONDS = 86400.0


def retrievability(stability: float, last_review_at: Optional[datetime], at: datetime) -> float:
    """Predicted chance of recalling a card at `at`; 0 for a card never reviewed."""
    if last_review_at is None or stability <= 0:
        return 0.0
    days = max(0.0, (at - last_review_at).total_seconds() / _DAY_SECONDS)
    return (1 + FACTOR * days / stability) ** DECAY


def mean(values: Iterable[float]) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def local_day(moment: datetime, zone: tzinfo) -> date:
    return moment.astimezone(zone).date()


def week_start(day: date) -> date:
    """The Monday of `day`'s week."""
    return day - timedelta(days=day.weekday())


def local_midnight(day: date, zone: tzinfo) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=zone)


def streak(answer_days: set[date], today: date) -> int:
    """Consecutive days with at least one answer, ending today; when nothing was
    answered yet today, ending yesterday (the streak is not broken until the
    day is over)."""
    day = today if today in answer_days else today - timedelta(days=1)
    count = 0
    while day in answer_days:
        count += 1
        day -= timedelta(days=1)
    return count
