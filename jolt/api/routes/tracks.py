"""Track routes — list / create / rename / archive, Track detail, agenda lock,
coverage (LLD §7.5, BE-07); the curated catalog, enrolment and track content
(library, decision tree) (BE-09).

`GET /v1/tracks` and `GET /v1/tracks/{id}` are the Tracks and Track detail
screens in one call each (`jolt.services.read_models`): a fixed handful of
single-partition reads, whatever the number of concepts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field, model_validator

from jolt.api.deps import RuntimeDep, UserDep
from jolt.api.errors import CONFLICT, NOT_FOUND, UNAUTHORIZED
from jolt.domain.agenda import MAX_OUTLINE_CHARS
from jolt.domain.models import Agenda, TrackKind
from jolt.services.curated import CuratedError, CuratedNotFound, CuratedService
from jolt.services.ownership import NotOwned
from jolt.services.read_models import ReadModelService, TrackDetail, TrackSummary
from jolt.services.tracks import TrackError, TrackService

router = APIRouter(prefix="/v1/tracks", tags=["tracks"], responses=UNAUTHORIZED)

AgendaSourceName = Literal["agent", "user", "emergent"]
ConceptStateName = Literal["new", "learning", "due", "mastered", "not_covered"]


class SyllabusItemResponse(BaseModel):
    concept_key: str
    label: str
    parent: str | None = None
    source: AgendaSourceName = Field(
        description="Who added the item: the learner, an agent's refine, or auto-add "
        "from uploads (emergent)"
    )


class AgendaResponse(BaseModel):
    status: str
    source: str
    syllabus: list[SyllabusItemResponse]
    last_refined_at: str | None = None
    refined_from_source_id: str | None = None
    outline: str | None = Field(
        default=None, description="The learner's outline from track creation, if any"
    )


def _agenda_response(agenda: Agenda) -> AgendaResponse:
    return AgendaResponse(
        status=agenda.status.value,
        source=agenda.source.value,
        syllabus=[
            SyllabusItemResponse(
                concept_key=i.concept_key,
                label=i.label,
                parent=i.parent,
                source=agenda.item_source(i).value,
            )
            for i in agenda.syllabus
        ],
        last_refined_at=agenda.last_refined_at.isoformat() if agenda.last_refined_at else None,
        refined_from_source_id=agenda.refined_from_source_id,
        outline=agenda.outline,
    )


class TrackResponse(BaseModel):
    """A track with its counts (the Tracks screen)."""

    id: str
    name: str
    origin: str
    syllabus: list[str]
    agenda: AgendaResponse
    user_id: str | None
    kind: Literal["notes", "dsa_patterns"]
    archived: bool
    archived_at: Optional[datetime]
    concept_count: int
    due_count: int = Field(description="Concepts in the review queue now (0 when archived)")
    source_count: int = Field(description="Confirmed uploads on the track")
    mastered_count: int
    retention: float = Field(description="Mean predicted recall of reviewed concepts, 0–1")
    description: Optional[str] = Field(description="Curated tracks: the catalog blurb; else null")
    enrolled: bool = Field(
        description="Curated tracks: the caller enrolled in it (always false on their own)"
    )
    created_at: datetime


def _track_response(s: TrackSummary) -> TrackResponse:
    t = s.track
    return TrackResponse(
        id=t.id,
        name=t.name,
        origin=t.origin.value,
        syllabus=[i.label for i in t.agenda.syllabus],
        agenda=_agenda_response(t.agenda),
        user_id=t.user_id,
        kind=t.kind.value,
        archived=t.archived_at is not None,
        archived_at=t.archived_at,
        concept_count=s.concept_count,
        due_count=s.due_count,
        source_count=s.source_count,
        mastered_count=s.mastered_count,
        retention=s.retention,
        description=t.description,
        enrolled=s.enrolled,
        created_at=t.created_at,
    )


class CreateTrackRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    syllabus: list[str] = Field(
        default=[], description="Topic labels: a user-authored, `refined` agenda"
    )
    kind: Literal["notes"] = Field(
        default="notes", description="Learners create notes tracks; DSA tracks are curated"
    )
    outline: Optional[str] = Field(
        default=None,
        max_length=MAX_OUTLINE_CHARS,
        description="A rough outline (one topic per line, indent to nest): a `draft` "
        "agenda the model tidies at the next sync",
    )

    @model_validator(mode="after")
    def _one_shape(self) -> "CreateTrackRequest":
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("name must not be blank")
        if self.outline and self.outline.strip() and self.syllabus:
            raise ValueError("send either an outline or a syllabus, not both")
        return self


class UpdateTrackRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    archived: Optional[bool] = None

    @model_validator(mode="after")
    def _not_blank(self) -> "UpdateTrackRequest":
        if self.name is not None:
            self.name = self.name.strip()
            if not self.name:
                raise ValueError("name must not be blank")
        return self


class FoundInResponse(BaseModel):
    source_id: str
    source_title: str
    uploaded_at: datetime


class TrackItemResponse(BaseModel):
    concept_key: str
    label: str
    parent: Optional[str]
    source: AgendaSourceName
    off_plan: bool = Field(
        description="On a locked track, outside the syllabus: kept, not counted in coverage"
    )
    concept_id: Optional[str]
    concept_title: Optional[str]
    state: ConceptStateName
    next_review_at: Optional[datetime]
    question_count: int
    question_version: Optional[int]
    found_in: Optional[FoundInResponse]


class DueTodayResponse(BaseModel):
    concept_id: str
    title: str
    question_id: str
    last_answer: Optional[str]


class RecentSourceResponse(BaseModel):
    id: str
    title: str
    kind: Literal["photos", "pdf", "file"]
    page_count: int
    uploaded_at: datetime


class TrackDetailResponse(TrackResponse):
    items: list[TrackItemResponse] = Field(
        description="Syllabus order: one row per concept on an item (an item with none is "
        "`not_covered`), then concepts outside the syllabus"
    )
    due_today: list[DueTodayResponse] = Field(description="Most overdue first")
    recent_sources: list[RecentSourceResponse] = Field(description="Newest first, up to 5")
    off_plan_count: int


def _detail_response(d: TrackDetail) -> TrackDetailResponse:
    return TrackDetailResponse(
        **_track_response(d).model_dump(),
        items=[
            TrackItemResponse(
                concept_key=i.concept_key,
                label=i.label,
                parent=i.parent,
                source=i.source,
                off_plan=i.off_plan,
                concept_id=i.concept_id,
                concept_title=i.concept_title,
                state=i.state,
                next_review_at=i.next_review_at,
                question_count=i.question_count,
                question_version=i.question_version,
                found_in=(
                    FoundInResponse(
                        source_id=i.found_in.source_id,
                        source_title=i.found_in.source_title,
                        uploaded_at=i.found_in.uploaded_at,
                    )
                    if i.found_in
                    else None
                ),
            )
            for i in d.items
        ],
        due_today=[DueTodayResponse(**x.__dict__) for x in d.due_today],
        recent_sources=[RecentSourceResponse(**x.__dict__) for x in d.recent_sources],
        off_plan_count=d.off_plan_count,
    )


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
    off_plan_concepts: int = Field(
        description="On a locked track, outside the syllabus: left out of the other counts"
    )
    coverage_ratio: float
    by_syllabus: list[SyllabusCoverageResponse]
    uncovered_syllabus: list[str]


@router.get("", response_model=list[TrackResponse])
async def list_tracks(user: UserDep, rt: RuntimeDep) -> list[TrackResponse]:
    """The caller's tracks (newest first, archived ones included and flagged), then
    curated ones, each with its counts."""
    summaries = await ReadModelService(rt).list_tracks(
        user.user_id, user.user.settings, user.user.enrolled_track_ids()
    )
    return [_track_response(s) for s in summaries]


# Declared before /{track_id} so "catalog" is never taken for a track id.
@router.get("/catalog", response_model=list[TrackResponse])
async def track_catalog(user: UserDep, rt: RuntimeDep) -> list[TrackResponse]:
    """Curated tracks anyone can add (BE-09), with `enrolled` for the caller."""
    summaries = await ReadModelService(rt).list_tracks(
        user.user_id, user.user.settings, user.user.enrolled_track_ids(), curated_only=True
    )
    return [_track_response(s) for s in summaries]


@router.post("", response_model=TrackResponse)
async def create_track(body: CreateTrackRequest, user: UserDep, rt: RuntimeDep) -> TrackResponse:
    t = await TrackService(rt).create_track(
        user.user_id, body.name, body.syllabus, kind=TrackKind(body.kind), outline=body.outline
    )
    return _track_response(TrackSummary(track=t))


@router.get("/{track_id}", response_model=TrackDetailResponse, responses=NOT_FOUND)
async def get_track(track_id: str, user: UserDep, rt: RuntimeDep) -> TrackDetailResponse:
    """Track detail: agenda, per-item concept state, due today, recent sources."""
    try:
        detail = await ReadModelService(rt).track_detail(
            user.user_id, track_id, user.user.settings, user.user.enrolled_track_ids()
        )
    except NotOwned as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _detail_response(detail)


@router.patch("/{track_id}", response_model=TrackResponse, responses={**NOT_FOUND, **CONFLICT})
async def update_track(
    track_id: str, body: UpdateTrackRequest, user: UserDep, rt: RuntimeDep
) -> TrackResponse:
    """Rename and/or archive one of the caller's own tracks. Archiving pauses it:
    its concepts leave the review queue until it is unarchived."""
    try:
        t = await TrackService(rt).update_track(
            user.user_id, track_id, name=body.name, archived=body.archived
        )
    except TrackError as exc:
        status = 404 if str(exc) == "track not found" else 409
        raise HTTPException(status_code=status, detail=str(exc))
    summary = await ReadModelService(rt).track_summary(
        user.user_id, t, user.user.settings, user.user.enrolled_track_ids()
    )
    return _track_response(summary)


@router.post("/{track_id}/agenda/lock", response_model=AgendaResponse, responses=NOT_FOUND)
async def lock_agenda(track_id: str, user: UserDep, rt: RuntimeDep) -> AgendaResponse:
    """Freeze refinement: subsequent set_agenda and auto-add are rejected/skipped."""
    try:
        agenda = await TrackService(rt).set_lock(user.user_id, track_id, True)
    except TrackError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _agenda_response(agenda)


@router.post("/{track_id}/agenda/unlock", response_model=AgendaResponse, responses=NOT_FOUND)
async def unlock_agenda(track_id: str, user: UserDep, rt: RuntimeDep) -> AgendaResponse:
    """Resume refinement after a lock."""
    try:
        agenda = await TrackService(rt).set_lock(user.user_id, track_id, False)
    except TrackError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _agenda_response(agenda)


@router.get("/{track_id}/coverage", response_model=CoverageResponse, responses=NOT_FOUND)
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
        off_plan_concepts=report.off_plan_concepts,
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


# --------------------------------------------------------------------------- #
# Curated tracks: enrolment and content (BE-09)
# --------------------------------------------------------------------------- #
CURATED_NOT_FOUND = {404: {**NOT_FOUND[404], "description": "Not a curated track"}}


class EnrolmentResponse(BaseModel):
    track_id: str
    enrolled: Literal[True] = True
    enrolled_at: datetime


@router.post(
    "/{track_id}/enrol",
    response_model=EnrolmentResponse,
    responses={**CURATED_NOT_FOUND, **CONFLICT},
)
async def enrol_track(track_id: str, user: UserDep, rt: RuntimeDep) -> EnrolmentResponse:
    """Add a curated track. Idempotent (enrolling again returns the first
    `enrolled_at`). Cards are created lazily, by the first review."""
    try:
        e = await CuratedService(rt).enrol(user.user_id, track_id)
    except CuratedNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except CuratedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return EnrolmentResponse(track_id=e.track_id, enrolled_at=e.enrolled_at)


@router.delete(
    "/{track_id}/enrol",
    status_code=204,
    response_class=Response,
    responses={**CURATED_NOT_FOUND, **CONFLICT},
)
async def unenrol_track(track_id: str, user: UserDep, rt: RuntimeDep) -> Response:
    """Leave a curated track. Idempotent. The caller's cards and answers stay
    (history) but the track's concepts leave the review queue until they enrol again."""
    try:
        await CuratedService(rt).unenrol(user.user_id, track_id)
    except CuratedNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except CuratedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return Response(status_code=204)


class PatternLibraryResponse(BaseModel):
    category: str
    time: str
    space: str
    when_to_reach: str
    tells: list[str]
    pseudocode: list[str]
    explanation: list[str] = Field(description="A few short paragraphs: how it works")
    animation_id: Optional[str] = Field(
        description="The step animation (the v1 reel, keyed by this slug); null if none"
    )


class LibraryPatternResponse(BaseModel):
    concept_id: str
    concept_key: str = Field(description="The pattern's slug (the syllabus key)")
    name: str = Field(description="The pattern, as the answer grid shows it")
    problem_count: int = Field(description="Playable problems whose answer is this pattern")
    library: PatternLibraryResponse


class LibraryResponse(BaseModel):
    track_id: str
    patterns: list[LibraryPatternResponse] = Field(description="Syllabus order")


@router.get("/{track_id}/library", response_model=LibraryResponse, responses=CURATED_NOT_FOUND)
async def track_library(track_id: str, user: UserDep, rt: RuntimeDep) -> LibraryResponse:
    """A DSA track's pattern library: when to reach for each pattern, the tells,
    pseudocode, a walkthrough and its animation (BE-09). Readable without enrolling."""
    try:
        track, concepts, content = await CuratedService(rt).library(track_id)
    except CuratedNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    counts = CuratedService.problem_counts(content)
    return LibraryResponse(
        track_id=track.id,
        patterns=[
            LibraryPatternResponse(
                concept_id=c.id,
                concept_key=c.syllabus_ref or c.id,
                name=c.title or c.text,
                problem_count=counts.get(c.id, 0),
                library=PatternLibraryResponse(**c.library.model_dump()),
            )
            for c in concepts
            if c.library is not None
        ],
    )


class DecisionTreeNodeResponse(BaseModel):
    id: str
    kind: Literal["question", "leaf"]
    prompt: Optional[str] = Field(default=None, description="question: asked in the guided walk")
    short: Optional[str] = Field(default=None, description="question: the flowchart label")
    yes: Optional[str] = Field(default=None, description="question: node id on yes")
    no: Optional[str] = Field(default=None, description="question: node id on no")
    pattern: Optional[str] = Field(default=None, description="leaf: the pattern it lands on")
    note: Optional[str] = Field(default=None, description="leaf: why")
    concept_id: Optional[str] = Field(
        default=None, description="leaf: the pattern's concept on this track, when it is one"
    )


class DecisionTreeResponse(BaseModel):
    track_id: str
    root: str
    nodes: list[DecisionTreeNodeResponse] = Field(description="In authoring order")


@router.get(
    "/{track_id}/decision-tree", response_model=DecisionTreeResponse, responses=CURATED_NOT_FOUND
)
async def track_decision_tree(track_id: str, user: UserDep, rt: RuntimeDep) -> DecisionTreeResponse:
    """The DSA decision tree, free (decision D3): yes/no questions that walk to a
    pattern (BE-09). Readable without enrolling."""
    service = CuratedService(rt)
    try:
        track, concepts, content = await service.library(track_id)
    except CuratedNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    by_name = {c.title: c.id for c in concepts if c.title}
    tree = content.decision_tree or {}
    nodes = []
    for node_id, node in (tree.get("nodes") or {}).items():
        pattern = node.get("pattern")
        base = pattern.split(" (")[0].strip() if isinstance(pattern, str) else None
        nodes.append(
            DecisionTreeNodeResponse(
                id=node.get("id") or node_id,
                kind=node.get("kind"),
                prompt=node.get("prompt"),
                short=node.get("short"),
                yes=node.get("yes"),
                no=node.get("no"),
                pattern=pattern,
                note=node.get("note"),
                concept_id=by_name.get(base) if base else None,
            )
        )
    return DecisionTreeResponse(track_id=track.id, root=tree.get("root") or "", nodes=nodes)
