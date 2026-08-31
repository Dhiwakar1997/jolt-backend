"""MCP read tools: get_tracks, get_recent_concepts, get_coverage (LLD §4).

Each tool resolves the connection's user before any repository call (scope
enforcement, §5) and returns plain JSON-serialisable dicts.
"""

from __future__ import annotations

from jolt.mcp.context import get_runtime, require_user
from jolt.services.tracks import TrackError, TrackService


def _agenda_dict(agenda) -> dict:
    return {
        "status": agenda.status.value,
        "source": agenda.source.value,
        "syllabus": [
            {"concept_key": i.concept_key, "label": i.label, "parent": i.parent}
            for i in agenda.syllabus
        ],
        "last_refined_at": (
            agenda.last_refined_at.isoformat() if agenda.last_refined_at else None
        ),
        "refined_from_source_id": agenda.refined_from_source_id,
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
                # Flat labels for convenience; `agenda` carries the full shape.
                "syllabus": [i.label for i in t.agenda.syllabus],
                "agenda": _agenda_dict(t.agenda),
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
    return _agenda_dict(agenda)


async def get_recent_concepts(track_id: str, limit: int = 20) -> dict:
    user = await require_user()
    rt = get_runtime()
    concepts = await rt.repos.concepts.list_for_track(track_id)
    concepts = sorted(concepts, key=lambda c: c.created_at, reverse=True)[:limit]
    return {
        "concepts": [
            {"id": c.id, "text": c.text, "syllabus_ref": c.syllabus_ref}
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
        "coverage_ratio": report.coverage_ratio,
        "uncovered_syllabus": report.uncovered_syllabus,
    }
