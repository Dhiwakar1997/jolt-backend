from jolt.domain.grading import GradingInput, apply_grading, score_stage_two
from jolt.domain.models import FSRSGrade, Question, Review, ReviewStatus


def _review() -> Review:
    return Review(user_id="u1", question_id="q1", concept_id="c1", free_text="a fairly full answer")


def _question() -> Question:
    return Question(
        id="q1",
        concept_id="c1",
        stem="?",
        options=["a", "b", "c"],
        correct_index=1,
        expected_answer="b",
    )


def test_score_stage_two_correct_sets_provisional_grade():
    review, question = _review(), _question()
    outcome = score_stage_two(review, question, selected_index=1, latency_ms=5000)
    assert outcome.is_correct is True
    assert review.provisional_fsrs_grade is not None
    assert review.selected_index == 1


def test_score_stage_two_incorrect_is_again():
    review, question = _review(), _question()
    outcome = score_stage_two(review, question, selected_index=0, latency_ms=5000)
    assert outcome.is_correct is False
    assert review.provisional_fsrs_grade == FSRSGrade.AGAIN.value


def test_apply_grading_is_idempotent():
    review = _review()
    grading = GradingInput(review_id=review.id, suggested_fsrs_grade=FSRSGrade.GOOD.value)

    first = apply_grading(review, grading)
    assert first.applied is True
    assert first.review.status == ReviewStatus.GRADED
    assert first.feedback is not None

    second = apply_grading(first.review, grading)
    assert second.applied is False
    assert second.feedback is None
