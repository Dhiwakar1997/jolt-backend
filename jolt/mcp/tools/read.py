"""MCP read tools: get_tracks, get_recent_concepts, get_coverage, get_agenda,
get_pending_corrections (LLD §4, BE-07).

Each tool resolves the connection's user before any repository call (scope
enforcement, §5) and returns plain JSON-serialisable dicts. Track-keyed tools
accept the caller's own tracks and curated ones, and only ever show concepts
the caller holds a card for (`jolt.services.ownership`).
"""

from __future__ import annotations

from jolt.domain.models import Agenda
from jolt.mcp.context import get_runtime, require_user
from jolt.services.corrections import CorrectionService
from jolt.services.ownership import NotOwned, visible_concepts
from jolt.services.tracks import TrackError, TrackService


def agenda_dict(agenda: Agenda) -> dict:
    return {
        "status": agenda.status.value,
        "source": agenda.source.value,
        "syllabus": [
            {
                "concept_key": i.concept_key,
                "label": i.label,
                "parent": i.parent,
                "source": agenda.item_source(i).value,
            }
            for i in agenda.syllabus
        ],
        "last_refined_at": (agenda.last_refined_at.isoformat() if agenda.last_refined_at else None),
        "refined_from_source_id": agenda.refined_from_source_id,
        "outline": agenda.outline,
    }


async def get_tracks() -> dict:
    user = await require_user()
    tracks = await TrackService(get_runtime()).list_tracks(user.user_id)
    return {
        "tracks": [
            {
                "id": t.id,
                "name": t.name,
                "origin": t.origin.value,
                "kind": t.kind.value,
                "archived": t.archived_at is not None,
                # Flat labels for convenience; `agenda` carries the full shape.
                "syllabus": [i.label for i in t.agenda.syllabus],
                "agenda": agenda_dict(t.agenda),
            }
            for t in tracks
        ]
    }


async def get_agenda(track_id: str) -> dict:
    user = await require_user()
    try:
        agenda = await TrackService(get_runtime()).get_agenda(user.user_id, track_id)
    except TrackError as exc:
        return {"error": str(exc)}
    return agenda_dict(agenda)


async def get_recent_concepts(track_id: str, limit: int = 20) -> dict:
    user = await require_user()
    try:
        concepts, _ = await visible_concepts(get_runtime(), user.user_id, track_id)
    except NotOwned as exc:
        raise ValueError(str(exc)) from exc
    concepts = sorted(concepts, key=lambda c: c.created_at, reverse=True)[: max(0, limit)]
    return {
        "concepts": [
            {"id": c.id, "text": c.text, "title": c.title, "syllabus_ref": c.syllabus_ref}
            for c in concepts
        ]
    }


async def get_coverage(track_id: str) -> dict:
    user = await require_user()
    report = await TrackService(get_runtime()).coverage(user.user_id, track_id)
    return {
        "track_id": report.track_id,
        "total_concepts": report.total_concepts,
        "studied_concepts": report.studied_concepts,
        "decaying_concepts": report.decaying_concepts,
        "off_plan_concepts": report.off_plan_concepts,
        "coverage_ratio": report.coverage_ratio,
        "uncovered_syllabus": report.uncovered_syllabus,
    }


async def get_pending_corrections() -> dict:
    """The learner's correction notes waiting for this sync (BE-07 §4)."""
    user = await require_user()
    pending = await CorrectionService(get_runtime()).pending(user.user_id)
    return {
        "corrections": [
            {
                "concept_id": p.concept_id,
                "track_id": p.track_id,
                "title": p.title,
                "text": p.text,
                "notes": [
                    {"note": n.note, "created_at": n.created_at.isoformat()} for n in p.notes
                ],
            }
            for p in pending
        ]
    }
