from datetime import datetime, timedelta, timezone

from jolt.domain import scheduling
from jolt.domain.models import ConceptState, FSRSGrade, Review, ReviewStatus


def test_synthesise_provisional_grade_incorrect_is_again():
    assert scheduling.synthesise_provisional_grade(False, 200, 5000) == FSRSGrade.AGAIN
    assert scheduling.synthesise_provisional_grade(False, 0, 5000) == FSRSGrade.AGAIN


def test_synthesise_provisional_grade_correct_is_good_whatever_length_or_speed():
    for length, latency in [(5, 1_000), (10, 5_000), (200, 3_000), (200, 60_000), (1, None)]:
        assert scheduling.synthesise_provisional_grade(True, length, latency) == FSRSGrade.GOOD


def test_synthesise_provisional_grade_correct_without_own_words_is_hard():
    assert scheduling.synthesise_provisional_grade(True, 0, 5000) == FSRSGrade.HARD
    assert scheduling.synthesise_provisional_grade(True, 0) == FSRSGrade.HARD


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


# --------------------------------------------------------------------------- #
# No learning / relearning steps, no fuzz (A52)
# --------------------------------------------------------------------------- #
T0 = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def _review(at: datetime, grade: FSRSGrade) -> Review:
    return Review(
        user_id="u1",
        question_id="q1",
        concept_id="c1",
        free_text_locked_at=at,
        provisional_fsrs_grade=grade.value,
    )


def _whole_days(state: ConceptState) -> float:
    return (state.due_at - state.last_review_at) / DAY


def test_first_answer_of_any_grade_is_due_in_whole_days_and_in_review():
    for grade in FSRSGrade:
        state = scheduling.apply_grade(
            ConceptState(user_id="u1", concept_id="c1"), grade, reviewed_at=T0
        )
        days = _whole_days(state)
        assert days >= 1 and days == int(days), (grade, days)
        assert state.fsrs_state == 2  # Review, never Learning
    good = scheduling.apply_grade(
        ConceptState(user_id="u1", concept_id="c1"), FSRSGrade.GOOD, reviewed_at=T0
    )
    assert _whole_days(good) == 3  # S0(Good) = 3.17 days at 0.9


def test_a_lapse_stays_in_review_and_is_due_in_a_day_or_more():
    folded = scheduling.fold_history(
        ConceptState(user_id="u1", concept_id="c1"),
        [_review(T0, FSRSGrade.GOOD), _review(T0 + 3 * DAY, FSRSGrade.AGAIN)],
    )
    assert folded.fsrs_state == 2 and folded.lapses == 1
    assert _whole_days(folded) >= 1 and _whole_days(folded) == int(_whole_days(folded))


def test_a_card_left_in_learning_by_an_older_build_moves_to_review():
    # What the default 1 min / 10 min steps used to persist after a first answer.
    stuck = ConceptState(
        user_id="u1",
        concept_id="c1",
        stability=3.173,
        difficulty=5.3,
        fsrs_state=1,
        step=1,
        last_review_at=T0,
        due_at=T0 + timedelta(minutes=10),
        reps=1,
    )
    stepped = scheduling.apply_grade(stuck, FSRSGrade.GOOD, reviewed_at=T0 + DAY)
    assert stepped.fsrs_state == 2 and _whole_days(stepped) >= 1
    relearning = stuck.model_copy(update={"fsrs_state": 3, "step": 0})
    stepped = scheduling.apply_grade(relearning, FSRSGrade.GOOD, reviewed_at=T0 + DAY)
    assert stepped.fsrs_state == 2 and _whole_days(stepped) >= 1
    # The next fold rebuilds from the history, which lands in Review too.
    folded = scheduling.fold_history(stuck, [_review(T0, FSRSGrade.GOOD)])
    assert folded.fsrs_state == 2 and folded.due_at == T0 + 3 * DAY


def test_refolding_the_same_history_gives_the_same_due_date():
    history = [_review(T0 + i * 4 * DAY, FSRSGrade.GOOD) for i in range(5)]
    first = scheduling.fold_history(ConceptState(user_id="u1", concept_id="c1"), history)
    for _ in range(5):
        again = scheduling.fold_history(ConceptState(user_id="u1", concept_id="c1"), history)
        assert again.due_at == first.due_at
    assert _whole_days(first) > 2.5  # long enough that fuzz would have applied


def test_desired_retention_only_changes_the_interval():
    strict = scheduling.apply_grade(
        ConceptState(user_id="u1", concept_id="c1"),
        FSRSGrade.GOOD,
        reviewed_at=T0,
        desired_retention=0.97,
    )
    relaxed = scheduling.apply_grade(
        ConceptState(user_id="u1", concept_id="c1"),
        FSRSGrade.GOOD,
        reviewed_at=T0,
        desired_retention=0.80,
    )
    assert strict.stability == relaxed.stability
    assert (_whole_days(strict), _whole_days(relaxed)) == (1, 8)
