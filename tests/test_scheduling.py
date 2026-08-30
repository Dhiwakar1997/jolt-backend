from datetime import datetime, timedelta, timezone

from jolt.domain.models import ConceptState, FSRSGrade, Review, ReviewStatus
from jolt.domain import scheduling


def test_synthesise_provisional_grade_incorrect_is_again():
    assert scheduling.synthesise_provisional_grade(False, 200, 5000) == FSRSGrade.AGAIN


def test_synthesise_provisional_grade_thorough_and_fast_is_easy():
    assert scheduling.synthesise_provisional_grade(True, 200, 5000) == FSRSGrade.EASY


def test_synthesise_provisional_grade_thin_is_hard():
    assert scheduling.synthesise_provisional_grade(True, 10, 5000) == FSRSGrade.HARD


def test_synthesise_provisional_grade_default_good():
    # Thorough but middling speed -> Good.
    assert scheduling.synthesise_provisional_grade(True, 200, 15000) == FSRSGrade.GOOD


def test_apply_grade_advances_card_and_sets_due():
    state = ConceptState(user_id="u1", concept_id="c1")
    updated = scheduling.apply_grade(state, FSRSGrade.GOOD)
    assert updated.reps == 1
    assert updated.due_at is not None
    assert updated.stability >= 0


def test_fold_history_prefers_final_grade():
    now = datetime.now(timezone.utc)
    state = ConceptState(user_id="u1", concept_id="c1")
    reviews = [
        Review(
            user_id="u1",
            question_id="q1",
            concept_id="c1",
            free_text_locked_at=now - timedelta(days=2),
            provisional_fsrs_grade=FSRSGrade.AGAIN.value,
            final_fsrs_grade=FSRSGrade.GOOD.value,  # final should win
            status=ReviewStatus.GRADED,
        ),
        Review(
            user_id="u1",
            question_id="q1",
            concept_id="c1",
            free_text_locked_at=now - timedelta(days=1),
            provisional_fsrs_grade=FSRSGrade.GOOD.value,
            status=ReviewStatus.PENDING_GRADING,
        ),
    ]
    folded = scheduling.fold_history(state, reviews)
    assert folded.reps == 2
    assert folded.dirty is False
    assert folded.due_at is not None
