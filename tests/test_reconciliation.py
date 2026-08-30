from jolt.domain.models import Concept
from jolt.domain.reconciliation import ChangeClass, ProposedConcept, reconcile


def _concept(text: str) -> Concept:
    return Concept(track_id="t1", text=text)


def test_unchanged_when_identical():
    existing = [_concept("Photosynthesis converts light energy into chemical energy.")]
    proposed = [ProposedConcept(text="Photosynthesis converts light energy into chemical energy.")]
    result = reconcile(existing, proposed)
    assert len(result.of(ChangeClass.UNCHANGED)) == 1


def test_new_when_no_match():
    existing = [_concept("The mitochondrion is the powerhouse of the cell.")]
    proposed = [ProposedConcept(text="Kirchhoff's voltage law sums loop voltages to zero.")]
    result = reconcile(existing, proposed)
    assert len(result.of(ChangeClass.NEW)) == 1
    assert len(result.of(ChangeClass.REMOVED)) == 1


def test_refined_on_minor_edit():
    existing = [_concept("Newton's second law states that force equals mass times acceleration.")]
    proposed = [
        ProposedConcept(
            text="Newton's second law states that net force equals mass times acceleration."
        )
    ]
    result = reconcile(existing, proposed)
    kinds = {d.change for d in result.deltas}
    assert ChangeClass.REFINED in kinds or ChangeClass.UNCHANGED in kinds


def test_removed_when_existing_unmatched():
    existing = [_concept("Alpha decay emits a helium nucleus.")]
    proposed = []
    result = reconcile(existing, proposed)
    assert len(result.of(ChangeClass.REMOVED)) == 1
