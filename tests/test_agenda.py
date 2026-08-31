"""Track agenda model tests (schema migration, agenda writes, concurrency).

These exercise the pure migration and the TrackService write paths against an
in-memory fake tracks repository that mimics Cosmos etag optimistic concurrency —
no Azure needed, in keeping with the domain-pure test style.
"""

from types import SimpleNamespace

import pytest

from jolt.data.repositories.tracks import CURATED_BUCKET, TracksRepository, _migrate_agenda
from jolt.domain.models import (
    AgendaSource,
    AgendaStatus,
    SyllabusItem,
    TrackOrigin,
)
from jolt.services.tracks import AgendaLocked, TrackService


# --------------------------------------------------------------------------- #
# Read-time migration
# --------------------------------------------------------------------------- #
def test_migrate_wraps_flat_syllabus_as_refined():
    doc = {"id": "t1", "name": "Physics", "origin": "user", "syllabus": ["mechanics", "thermo"]}
    out = _migrate_agenda(doc)
    assert "syllabus" not in out  # flat field dropped
    assert out["agenda"]["status"] == AgendaStatus.REFINED.value
    assert out["agenda"]["source"] == "agent"
    assert out["agenda"]["syllabus"] == [
        {"concept_key": "mechanics", "label": "mechanics"},
        {"concept_key": "thermo", "label": "thermo"},
    ]


def test_migrate_curated_track_is_locked():
    doc = {"id": "t1", "name": "Curated", "origin": TrackOrigin.JOLT.value, "syllabus": ["a"]}
    out = _migrate_agenda(doc)
    assert out["agenda"]["status"] == AgendaStatus.LOCKED.value


def test_migrate_is_noop_when_agenda_present():
    doc = {"id": "t1", "name": "X", "origin": "user", "agenda": {"status": "draft", "syllabus": []}}
    out = _migrate_agenda(doc)
    assert out["agenda"]["status"] == "draft"


def test_from_doc_migrates_and_preserves_join_key():
    repo = TracksRepository(SimpleNamespace())  # gateway unused by _from_doc
    track = repo._from_doc(
        {"id": "t1", "name": "Bio", "origin": "user", "syllabus": ["cells"]}
    )
    assert track.agenda.status == AgendaStatus.REFINED
    # concept_key == label so existing concepts (syllabus_ref='cells') still join.
    assert track.agenda.syllabus[0].concept_key == "cells"


def test_from_doc_curated_bucket_maps_to_none_user():
    repo = TracksRepository(SimpleNamespace())
    track = repo._from_doc(
        {"id": "t1", "name": "C", "origin": "jolt", "user_id": CURATED_BUCKET, "syllabus": []}
    )
    assert track.user_id is None
    assert track.agenda.status == AgendaStatus.LOCKED


# --------------------------------------------------------------------------- #
# Fake repo with etag optimistic-concurrency semantics
# --------------------------------------------------------------------------- #
class FakeTracksRepo:
    def __init__(self, track):
        self._counter = 0
        self.fail_next_replace = 0
        self._put(track)

    def _put(self, track):
        self._counter += 1
        stored = track.model_copy(deep=True)
        stored.etag = f"etag-{self._counter}"
        self._stored = stored

    async def get_track(self, track_id, user_id):
        if track_id != self._stored.id:
            return None
        return self._stored.model_copy(deep=True)

    async def replace(self, model, *, etag=None):
        if self.fail_next_replace > 0:
            self.fail_next_replace -= 1
            self._put(self._stored)  # a concurrent writer bumped the etag
            return None
        if etag != self._stored.etag:
            return None
        self._put(model)
        return self._stored.model_copy(deep=True)


def _service(track):
    repo = FakeTracksRepo(track)
    rt = SimpleNamespace(repos=SimpleNamespace(tracks=repo))
    return TrackService(rt), repo


def _new_track(status=AgendaStatus.NONE, syllabus=None):
    from jolt.domain.models import Agenda, Track

    return Track(
        id="t1",
        user_id="u1",
        name="T",
        origin=TrackOrigin.USER,
        agenda=Agenda(status=status, syllabus=syllabus or []),
    )


# --------------------------------------------------------------------------- #
# set_agenda
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_set_agenda_replaces_and_marks_refined():
    svc, _ = _service(_new_track())
    items = [SyllabusItem(concept_key="k1", label="One")]
    agenda = await svc.set_agenda("u1", "t1", items, AgendaSource.AGENT, mark_refined=True)
    assert agenda.status == AgendaStatus.REFINED
    assert agenda.last_refined_at is not None
    assert [i.concept_key for i in agenda.syllabus] == ["k1"]


@pytest.mark.asyncio
async def test_set_agenda_replace_semantics_no_double_apply():
    svc, _ = _service(_new_track())
    items = [SyllabusItem(concept_key="k1", label="One")]
    await svc.set_agenda("u1", "t1", items, AgendaSource.AGENT)
    agenda = await svc.set_agenda("u1", "t1", items, AgendaSource.AGENT)  # replay
    assert [i.concept_key for i in agenda.syllabus] == ["k1"]  # not doubled


@pytest.mark.asyncio
async def test_set_agenda_rejected_when_locked():
    svc, _ = _service(_new_track(status=AgendaStatus.LOCKED))
    with pytest.raises(AgendaLocked):
        await svc.set_agenda("u1", "t1", [], AgendaSource.AGENT)


@pytest.mark.asyncio
async def test_set_agenda_retries_on_etag_conflict():
    svc, repo = _service(_new_track())
    repo.fail_next_replace = 1  # first write loses the race, retry must succeed
    items = [SyllabusItem(concept_key="k1", label="One")]
    agenda = await svc.set_agenda("u1", "t1", items, AgendaSource.AGENT, mark_refined=True)
    assert [i.concept_key for i in agenda.syllabus] == ["k1"]


# --------------------------------------------------------------------------- #
# auto_add
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_auto_add_adds_missing_keys_and_is_idempotent():
    svc, _ = _service(_new_track(syllabus=[SyllabusItem(concept_key="k1", label="One")]))
    items = [
        SyllabusItem(concept_key="k1", label="One"),  # already present
        SyllabusItem(concept_key="k2", label="Two"),  # new
    ]
    agenda = await svc.auto_add_concepts("u1", "t1", items)
    assert [i.concept_key for i in agenda.syllabus] == ["k1", "k2"]
    # Re-run: keyed on membership, so nothing is duplicated.
    agenda = await svc.auto_add_concepts("u1", "t1", items)
    assert [i.concept_key for i in agenda.syllabus] == ["k1", "k2"]


@pytest.mark.asyncio
async def test_auto_add_nests_under_resolvable_parent_only():
    svc, _ = _service(_new_track(syllabus=[SyllabusItem(concept_key="root", label="Root")]))
    items = [
        SyllabusItem(concept_key="child", label="Child", parent="root"),
        SyllabusItem(concept_key="orphan", label="Orphan", parent="missing"),
    ]
    agenda = await svc.auto_add_concepts("u1", "t1", items)
    by_key = {i.concept_key: i for i in agenda.syllabus}
    assert by_key["child"].parent == "root"
    assert by_key["orphan"].parent is None  # unresolvable parent dropped


@pytest.mark.asyncio
async def test_auto_add_skipped_when_locked():
    svc, _ = _service(_new_track(status=AgendaStatus.LOCKED))
    agenda = await svc.auto_add_concepts(
        "u1", "t1", [SyllabusItem(concept_key="k1", label="One")]
    )
    assert agenda.syllabus == []  # locked: auto-add skipped, no error


# --------------------------------------------------------------------------- #
# lock / unlock
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_lock_then_unlock_restores_refined():
    svc, _ = _service(_new_track(syllabus=[SyllabusItem(concept_key="k1", label="One")]))
    locked = await svc.set_lock("u1", "t1", True)
    assert locked.status == AgendaStatus.LOCKED
    unlocked = await svc.set_lock("u1", "t1", False)
    assert unlocked.status == AgendaStatus.REFINED  # has syllabus


@pytest.mark.asyncio
async def test_unlock_empty_agenda_becomes_none():
    svc, _ = _service(_new_track(status=AgendaStatus.LOCKED))
    unlocked = await svc.set_lock("u1", "t1", False)
    assert unlocked.status == AgendaStatus.NONE
