"""`GET /v1/progress`: the Progress screen in one read (BE-08).

Every number is computed from the user's own partitions (concept states,
reviews, sources, tracks) when asked; nothing is stored. Days are the learner's
days (`time_zone` setting).

Definitions (also in docs/API.md):

* **studied**: the concept has at least one graded answer (FSRS `reps > 0`).
* **mastered**: `mastered_at` is set (BE-06 rule); **retired**: mastered and out
  of the queue under the user's `retire_mastered` setting.
* **active** (the retention population): studied and not retired.
* **learning**: studied and not mastered. Learning and mastered are disjoint.
* **due**: not retired and `due_at` <= now (never-studied concepts included).
* **retention**: mean predicted recall (`jolt.domain.progress.retrievability`)
  of the active concepts now. **retention_weekly**: the same mean at the end of
  each week (now for the current week), from each card's stability after each
  of its answers; a concept counts from its first answer, and not once it is
  retired (`mastered_at` on or before that time). Weeks with no active concept
  are left out.
* **answers**: reviews with stage 2 done, dated by their stage 2 time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from jolt.domain.mastery import is_retired
from jolt.domain.models import ConceptState, Review, Track
from jolt.domain.progress import (
    local_day,
    local_midnight,
    mean,
    retrievability,
    streak,
    week_start,
)
from jolt.domain.scheduling import stability_snapshots
from jolt.domain.zones import user_zone
from jolt.runtime import Runtime

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class WeekPoint:
    week_start: date
    retention: float


@dataclass
class DayCount:
    date: date
    reviews: int


@dataclass
class TrackRow:
    track_id: str
    name: str
    kind: str
    retention: float
    due: int
    mastered: int
    learning: int


@dataclass
class Progress:
    weeks: int
    retention: float
    retention_weekly: list[WeekPoint] = field(default_factory=list)
    reviews_total: int = 0
    reviews_this_week: int = 0
    answers_in_own_words: int = 0
    streak_days: int = 0
    concepts_learning: int = 0
    concepts_mastered: int = 0
    sources_read: int = 0
    daily: list[DayCount] = field(default_factory=list)
    tracks: list[TrackRow] = field(default_factory=list)


def _answered_at(review: Review) -> Optional[datetime]:
    if review.selected_index is None:
        return None
    return review.stage2_at or review.free_text_locked_at or review.created_at


class ProgressService:
    def __init__(self, rt: Runtime) -> None:
        self._rt = rt

    async def _tracks(
        self, user_id: str, states: list[ConceptState], enrolled: set[str]
    ) -> list[Track]:
        """The user's tracks (newest first), then the curated tracks they are
        enrolled in (BE-09) or have cards on (studied before leaving)."""
        tracks = await self._rt.repos.tracks.list_for_user(user_id)
        seen = {t.id for t in tracks}
        curated = await self._rt.repos.tracks.list_curated()
        curated.sort(key=lambda t: (t.created_at, t.id))
        studied_on = {s.track_id for s in states if s.track_id}
        for track in curated:
            if track.id not in seen and (track.id in enrolled or track.id in studied_on):
                tracks.append(track)
                seen.add(track.id)
        return tracks

    async def progress(
        self, user_id: str, weeks: int = 26, *, now: Optional[datetime] = None
    ) -> Progress:
        now = now or _now()
        user = await self._rt.repos.users.get_user(user_id)
        settings = user.settings if user is not None else None
        retire_mastered = settings.retire_mastered if settings is not None else True
        zone = user_zone(settings)
        today = local_day(now, zone)

        states = await self._rt.repos.concept_states.all_for_user(user_id)
        enrolled = user.enrolled_track_ids() if user is not None else set()
        tracks = await self._tracks(user_id, states, enrolled)
        # Only concepts that are still active in their track (a superseded
        # concept's card no longer counts).
        active_ids: set[str] = set()
        for track in tracks:
            active_ids |= {c.id for c in await self._rt.repos.concepts.list_for_track(track.id)}
        states = [s for s in states if s.concept_id in active_ids]

        reviews = await self._rt.repos.reviews.created_since(user_id, _EPOCH)
        reviews.sort(key=lambda r: (r.created_at, r.id))

        # -- concept counts and retention now ---------------------------------
        def studied(s: ConceptState) -> bool:
            return s.reps > 0

        def retired(s: ConceptState) -> bool:
            return is_retired(s, retire_mastered)

        active = [s for s in states if studied(s) and not retired(s)]
        recall_now = {
            s.concept_id: retrievability(s.stability, s.last_review_at, now) for s in active
        }

        def is_due(s: ConceptState) -> bool:
            return not retired(s) and s.due_at is not None and s.due_at <= now

        rows: list[TrackRow] = []
        for track in tracks:
            mine = [s for s in states if s.track_id == track.id]
            rows.append(
                TrackRow(
                    track_id=track.id,
                    name=track.name,
                    kind=track.kind.value,
                    retention=mean(
                        recall_now.get(s.concept_id, 0.0)
                        for s in mine
                        if s.concept_id in recall_now
                    ),
                    due=sum(1 for s in mine if is_due(s)),
                    mastered=sum(1 for s in mine if s.mastered_at is not None),
                    learning=sum(1 for s in mine if studied(s) and s.mastered_at is None),
                )
            )

        # -- weekly retention -------------------------------------------------
        by_concept: dict[str, list[Review]] = {}
        for review in reviews:
            by_concept.setdefault(review.concept_id, []).append(review)
        snapshots = {
            s.concept_id: stability_snapshots(by_concept.get(s.concept_id, []))
            for s in states
            if studied(s)
        }
        current_week = week_start(today)
        weekly: list[WeekPoint] = []
        for i in range(weeks - 1, -1, -1):
            start = current_week - timedelta(weeks=i)
            at = min(local_midnight(start + timedelta(weeks=1), zone), now)
            values = []
            for s in states:
                if retired(s) and s.mastered_at is not None and s.mastered_at <= at:
                    continue
                last = None
                for reviewed_at, stability in snapshots.get(s.concept_id, []):
                    if reviewed_at > at:
                        break
                    last = (reviewed_at, stability)
                if last is not None:
                    values.append(retrievability(last[1], last[0], at))
            if values:
                weekly.append(WeekPoint(week_start=start, retention=mean(values)))

        # -- answers by day ---------------------------------------------------
        days = weeks * 7
        first_day = today - timedelta(days=days - 1)
        per_day: dict[date, int] = {}
        answer_days: set[date] = set()
        own_words = 0
        for review in reviews:
            at = _answered_at(review)
            if at is not None:
                day = local_day(at, zone)
                answer_days.add(day)
                if first_day <= day <= today:
                    per_day[day] = per_day.get(day, 0) + 1
            locked = review.free_text_locked_at
            if locked is not None and (review.free_text or "").strip():
                if first_day <= local_day(locked, zone) <= today:
                    own_words += 1
        window = [first_day + timedelta(days=i) for i in range(days)]
        daily = [DayCount(date=d, reviews=per_day.get(d, 0)) for d in window]

        sources = await self._rt.repos.sources.list_confirmed(user_id)
        return Progress(
            weeks=weeks,
            retention=mean(recall_now.values()),
            retention_weekly=weekly,
            reviews_total=sum(d.reviews for d in daily),
            reviews_this_week=sum(d.reviews for d in daily if d.date >= current_week),
            answers_in_own_words=own_words,
            streak_days=streak(answer_days, today),
            concepts_learning=sum(1 for s in states if studied(s) and s.mastered_at is None),
            concepts_mastered=sum(1 for s in states if s.mastered_at is not None),
            sources_read=sum(1 for s in sources if s.active_extraction_id),
            daily=daily,
            tracks=rows,
        )
