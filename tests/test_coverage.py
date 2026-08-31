from datetime import datetime, timedelta, timezone

from jolt.domain.coverage import compute_coverage
from jolt.domain.models import (
    Agenda,
    AgendaStatus,
    Concept,
    ConceptState,
    SyllabusItem,
    Track,
)


def _agenda(*labels: str, status: AgendaStatus = AgendaStatus.REFINED) -> Agenda:
    return Agenda(
        status=status,
        syllabus=[SyllabusItem(concept_key=label, label=label) for label in labels],
    )


def test_coverage_counts_studied_and_uncovered():
    track = Track(id="t1", name="Physics", agenda=_agenda("mechanics", "thermo"))
    concepts = [
        Concept(id="c1", track_id="t1", text="F=ma", syllabus_ref="mechanics"),
        Concept(id="c2", track_id="t1", text="PV=nRT", syllabus_ref="thermo"),
        Concept(id="c3", track_id="t1", text="momentum", syllabus_ref="mechanics"),
    ]
    states = [ConceptState(user_id="u1", concept_id="c1")]  # only c1 studied

    report = compute_coverage(track, concepts, states)
    assert report.total_concepts == 3
    assert report.studied_concepts == 1
    assert "thermo" in report.uncovered_syllabus
    assert "mechanics" not in report.uncovered_syllabus


def test_coverage_flags_decaying_by_due_date():
    now = datetime.now(timezone.utc)
    track = Track(id="t1", name="Bio", agenda=_agenda("cells"))
    concepts = [Concept(id="c1", track_id="t1", text="mitochondria", syllabus_ref="cells")]
    states = [
        ConceptState(
            user_id="u1", concept_id="c1", due_at=now - timedelta(days=3), retrievability=0.5
        )
    ]
    report = compute_coverage(track, concepts, states, now=now)
    assert report.decaying_concepts == 1
