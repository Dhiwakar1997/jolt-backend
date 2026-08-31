"""Track routes — list/create tracks, coverage (LLD §7.5)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from jolt.api.deps import RuntimeDep, UserDep
from jolt.domain.models import Agenda
from jolt.services.tracks import TrackError, TrackService

router = APIRouter(prefix="/v1/tracks", tags=["tracks"])


class SyllabusItemResponse(BaseModel):
    concept_key: str
    label: str
    parent: str | None = None


class AgendaResponse(BaseModel):
    status: str
    source: str
    syllabus: list[SyllabusItemResponse]
    last_refined_at: str | None = None
    refined_from_source_id: str | None = None


def _agenda_response(agenda: Agenda) -> AgendaResponse:
    return AgendaResponse(
        status=agenda.status.value,
        source=agenda.source.value,
        syllabus=[
            SyllabusItemResponse(concept_key=i.concept_key, label=i.label, parent=i.parent)
            for i in agenda.syllabus
        ],
        last_refined_at=agenda.last_refined_at.isoformat() if agenda.last_refined_at else None,
        refined_from_source_id=agenda.refined_from_source_id,
    )


class TrackResponse(BaseModel):
    id: str
    name: str
    origin: str
    syllabus: list[str]
    agenda: AgendaResponse
    user_id: str | None


def _track_response(t) -> TrackResponse:
    return TrackResponse(
        id=t.id,
        name=t.name,
        origin=t.origin.value,
        syllabus=[i.label for i in t.agenda.syllabus],
        agenda=_agenda_response(t.agenda),
        user_id=t.user_id,
    )


class CreateTrackRequest(BaseModel):
    name: str
    syllabus: list[str] = []


class SyllabusCoverageResponse(BaseModel):
    syllabus_ref: str
    total_concepts: int
    studied_concepts: int
    coverage_ratio: float


class CoverageResponse(BaseModel):
    track_id: str
    total_concepts: int
    studied_concepts: int
    decaying_concepts: int
    coverage_ratio: float
    by_syllabus: list[SyllabusCoverageResponse]
    uncovered_syllabus: list[str]


@router.get("", response_model=list[TrackResponse])
async def list_tracks(user: UserDep, rt: RuntimeDep) -> list[TrackResponse]:
    tracks = await TrackService(rt).list_tracks(user.user_id)
    return [_track_response(t) for t in tracks]


@router.post("", response_model=TrackResponse)
async def create_track(
    body: CreateTrackRequest, user: UserDep, rt: RuntimeDep
) -> TrackResponse:
    t = await TrackService(rt).create_track(user.user_id, body.name, body.syllabus)
    return _track_response(t)


@router.post("/{track_id}/agenda/lock", response_model=AgendaResponse)
async def lock_agenda(track_id: str, user: UserDep, rt: RuntimeDep) -> AgendaResponse:
    """Freeze refinement: subsequent set_agenda and auto-add are rejected/skipped."""
    try:
        agenda = await TrackService(rt).set_lock(user.user_id, track_id, True)
    except TrackError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _agenda_response(agenda)


@router.post("/{track_id}/agenda/unlock", response_model=AgendaResponse)
async def unlock_agenda(track_id: str, user: UserDep, rt: RuntimeDep) -> AgendaResponse:
    """Resume refinement after a lock."""
    try:
        agenda = await TrackService(rt).set_lock(user.user_id, track_id, False)
    except TrackError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _agenda_response(agenda)


@router.get("/{track_id}/coverage", response_model=CoverageResponse)
async def coverage(track_id: str, user: UserDep, rt: RuntimeDep) -> CoverageResponse:
    try:
        report = await TrackService(rt).coverage(user.user_id, track_id)
    except TrackError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return CoverageResponse(
        track_id=report.track_id,
        total_concepts=report.total_concepts,
        studied_concepts=report.studied_concepts,
        decaying_concepts=report.decaying_concepts,
        coverage_ratio=report.coverage_ratio,
        by_syllabus=[
            SyllabusCoverageResponse(
                syllabus_ref=s.syllabus_ref,
                total_concepts=s.total_concepts,
                studied_concepts=s.studied_concepts,
                coverage_ratio=s.coverage_ratio,
            )
            for s in report.by_syllabus
        ],
        uncovered_syllabus=report.uncovered_syllabus,
    )
