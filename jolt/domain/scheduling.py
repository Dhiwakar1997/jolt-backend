"""FSRS wrapper, grade synthesis, reschedule (LLD §4 scheduling, §7.3, §7.4).

Pure computation, in-process (LLD §1: no inference, no worker queue). This module
adapts the open-source `fsrs` library behind a small interface so the rest of the
domain speaks in `ConceptState` + `FSRSGrade`, never in library types.

Recompute is **inline, not a job**: `fold_history` is called directly from the
review-submission handler (§7.3, at stage-2 submit) and the grade-submission
handler (§7.2, `jolt_submit_gradings`), folding a single card's history in the
same request the grade arrives in. Folding one card is a handful of arithmetic
operations, so there is no scheduler and no timed FSRS job in the MVP.

The library's public class was renamed across major versions (`FSRS` → `Scheduler`)
and `Card` gained/lost fields; the adapter below normalises both so a minor
dependency bump does not ripple into callers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from jolt.config import get_settings
from jolt.domain.models import ConceptState, FSRSGrade, Review


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Library adapter
# --------------------------------------------------------------------------- #
def _load_fsrs(desired_retention: float | None = None):
    """Return (scheduler, Card, Rating) normalised across fsrs versions.

    `desired_retention` is the user's setting (BE-08); None means the server
    default, FSRS_DESIRED_RETENTION. It only changes the intervals (due dates),
    never stability or difficulty.
    """
    import fsrs as _f

    retention = desired_retention or get_settings().fsrs_desired_retention
    if hasattr(_f, "Scheduler"):  # py-fsrs >= 4.1
        # No learning or relearning steps (decision A52): the library defaults
        # (1 and 10 minutes) would make a first correct answer due again in
        # minutes. Jolt reviews daily and the nightly grading replaces the
        # provisional grade, so every answer schedules in whole days, at least
        # one, and `interval_days` always agrees with `due_at`. A card persisted
        # in Learning or Relearning by an older build goes straight to Review
        # on its next fold. No fuzzing either: the full-history fold reruns on
        # every grade, and a random fuzz would move the due date each time even
        # when the grade did not change.
        scheduler = _f.Scheduler(
            desired_retention=retention,
            learning_steps=(),
            relearning_steps=(),
            enable_fuzzing=False,
        )
        return scheduler, _f.Card, _f.Rating
    # py-fsrs 4.x
    scheduler = _f.FSRS()
    return scheduler, _f.Card, _f.Rating


def _card_from_state(state: ConceptState, Card):
    """Rebuild a library Card from persisted ConceptState fields."""
    card = Card()
    # Field names are stable across v4/v5 for these attributes.
    if state.stability:
        setattr(card, "stability", state.stability)
    if state.difficulty:
        setattr(card, "difficulty", state.difficulty)
    if hasattr(card, "state") and state.fsrs_state:
        try:
            import fsrs as _f

            card.state = _f.State(state.fsrs_state)
        except Exception:
            pass
    if hasattr(card, "step"):
        card.step = state.step
    if state.last_review_at and hasattr(card, "last_review"):
        card.last_review = state.last_review_at
    if state.due_at and hasattr(card, "due"):
        card.due = state.due_at
    return card


def _apply_card_to_state(state: ConceptState, card, reviewed_at: datetime) -> ConceptState:
    state.stability = float(getattr(card, "stability", 0.0) or 0.0)
    state.difficulty = float(getattr(card, "difficulty", 0.0) or 0.0)
    due = getattr(card, "due", None)
    state.due_at = due if isinstance(due, datetime) else state.due_at
    st = getattr(card, "state", None)
    state.fsrs_state = int(getattr(st, "value", st) or 0) if st is not None else state.fsrs_state
    state.step = int(getattr(card, "step", 0) or 0)
    state.last_review_at = reviewed_at
    return state


def apply_grade(
    state: ConceptState,
    grade: FSRSGrade,
    *,
    reviewed_at: datetime | None = None,
    desired_retention: float | None = None,
) -> ConceptState:
    """Advance one FSRS step and write the new card fields onto `state`."""
    reviewed_at = reviewed_at or _now()
    scheduler, Card, Rating = _load_fsrs(desired_retention)
    card = _card_from_state(state, Card)
    rating = Rating(grade.value)

    # v5: review_card(card, rating, review_datetime) -> (card, log)
    # v4: repeat(card, now) -> dict[Rating -> SchedulingInfo]
    if hasattr(scheduler, "review_card"):
        try:
            card, _log = scheduler.review_card(card, rating, reviewed_at)
        except TypeError:
            card, _log = scheduler.review_card(card, rating)
    else:  # v4
        scheduling = scheduler.repeat(card, reviewed_at)
        card = scheduling[rating].card

    state = _apply_card_to_state(state, card, reviewed_at)
    state.reps += 1
    if grade == FSRSGrade.AGAIN:
        state.lapses += 1
    return state


def fold_history(
    state: ConceptState,
    reviews: Iterable[Review],
    *,
    desired_retention: float | None = None,
) -> ConceptState:
    """Full-history FSRS fold (LLD §7.4).

    Rebuilds the card from scratch over the ordered review history, preferring a
    review's final (semantic) grade over its provisional one. Folding the whole
    history — rather than stepping incrementally — is what lets a retroactive
    final grade correct an earlier provisional one (spec §7).
    """
    # Reset transient card fields; keep identity + counters recomputed below.
    fresh = ConceptState(
        id=state.id,
        user_id=state.user_id,
        concept_id=state.concept_id,
        track_id=state.track_id,
        etag=state.etag,
    )
    for review in reviews:
        grade_value = review.final_fsrs_grade or review.provisional_fsrs_grade
        if grade_value is None:
            continue
        reviewed_at = review.free_text_locked_at or review.created_at
        fresh = apply_grade(
            fresh,
            FSRSGrade(grade_value),
            reviewed_at=reviewed_at,
            desired_retention=desired_retention,
        )
    fresh.dirty = False
    return fresh


def stability_snapshots(reviews: Iterable[Review]) -> list[tuple[datetime, float]]:
    """(reviewed_at, stability) after each graded review, oldest first (BE-08).

    The same fold as `fold_history`, kept step by step, so progress can ask
    "what was this card's predicted recall on date X" without refolding per
    date. Stability does not depend on desired retention, so none is passed.
    """
    state = ConceptState(user_id="", concept_id="")
    out: list[tuple[datetime, float]] = []
    for review in reviews:
        grade_value = review.final_fsrs_grade or review.provisional_fsrs_grade
        if grade_value is None:
            continue
        reviewed_at = review.free_text_locked_at or review.created_at
        state = apply_grade(state, FSRSGrade(grade_value), reviewed_at=reviewed_at)
        out.append((reviewed_at, state.stability))
    return out


# TODO(global-reparameterization): if the FSRS default parameters change in a
# future library upgrade, every card must be re-folded under the new weights. That
# is a rare, offline migration — a batch that iterates all users' concept_states
# and calls `fold_history` per card would hook in here. Deferred by design (LLD
# §11); build nothing for it until the parameters actually change.


# --------------------------------------------------------------------------- #
# Grade synthesis (LLD §7.3)
# --------------------------------------------------------------------------- #
# Provisional grade at stage-2 submit, before the semantic grade arrives via sync.
def synthesise_provisional_grade(
    is_correct: bool,
    stage1_len: int,
    latency_ms: int | None = None,
) -> FSRSGrade:
    """Map (correctness, own words) to a provisional FSRS rating (decision A52).

    * incorrect choice → AGAIN;
    * correct choice with no own words (`stage1_len` 0: blank after trimming,
      e.g. "I don't know yet") → HARD — recognised, not recalled;
    * any other correct choice → GOOD.

    `stage1_len` is the length of the stage-1 text after trimming whitespace.
    Length and speed beyond that say little about recall, so `latency_ms` is
    accepted but not used; the semantic final grade from the nightly sync (7.2)
    supersedes this one in the FSRS fold.
    """
    if not is_correct:
        return FSRSGrade.AGAIN
    if stage1_len <= 0:
        return FSRSGrade.HARD
    return FSRSGrade.GOOD
