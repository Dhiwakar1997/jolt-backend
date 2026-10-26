"""Nightly sync status, run history and the scheduled-task prompt (BE-04).

The sync itself happens over MCP (`jolt_sync` and the tools it plans); these
routes let the app see that it happened: Today's sync line and "New since last
sync", Settings · Model, and the onboarding "waiting for first sync" screen.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from jolt.api.deps import RuntimeDep, UserDep
from jolt.api.errors import UNAUTHORIZED
from jolt.domain.models import SyncRun
from jolt.domain.sync import next_expected, parse_schedule_time, sync_health
from jolt.services.sync_prompt import load_prompt
from jolt.services.sync_runs import SyncRunService

router = APIRouter(prefix="/v1/sync", tags=["sync"], responses=UNAUTHORIZED)


class SyncChangeResponse(BaseModel):
    kind: Literal["concept_added", "concept_updated", "question_rewritten"]
    concept_id: str
    concept_title: str
    track_id: str
    source_id: Optional[str] = Field(description="The upload the concept came from")
    source_uploaded_at: Optional[datetime] = Field(
        description='When that upload was made, for "Found in your 12 Sep notes"'
    )
    added_to_syllabus: bool = Field(description="The concept was also added to the track's agenda")


class SyncRunResponse(BaseModel):
    id: str
    started_at: datetime
    finished_at: Optional[datetime] = Field(description="null while the run is still going")
    client_label: str = Field(description='The MCP token\'s label, e.g. "Claude Desktop"')
    status: Literal["running", "finished", "incomplete"] = Field(
        description="finished: a final jolt_sync found nothing left; incomplete: the client "
        "went quiet first"
    )
    sources_processed: int
    questions_written: int
    gradings_submitted: int
    errors: list[str]
    changes: list[SyncChangeResponse] = Field(
        description="Concepts this run added or changed, in order"
    )


class SyncStatusResponse(BaseModel):
    last_run: Optional[SyncRunResponse]
    next_expected_at: Optional[datetime] = Field(
        description="The next scheduled time (SYNC_SCHEDULE_TIME in `tz`), in UTC"
    )
    health: Literal["ok", "late", "never"]


class SyncPromptResponse(BaseModel):
    prompt: str = Field(description="Markdown to paste into a Claude or Codex scheduled task")
    schedule_time: str = Field(description='Recommended local time, "HH:MM" (decision D5)')
    version: str


def run_response(run: SyncRun) -> SyncRunResponse:
    return SyncRunResponse(
        id=run.id,
        started_at=run.started_at,
        finished_at=run.finished_at,
        client_label=run.client_label,
        status=run.status.value,
        sources_processed=run.sources_processed,
        questions_written=run.questions_written,
        gradings_submitted=run.gradings_submitted,
        errors=run.errors,
        changes=[
            SyncChangeResponse(
                kind=c.kind.value,
                concept_id=c.concept_id,
                concept_title=c.concept_title,
                track_id=c.track_id,
                source_id=c.source_id,
                source_uploaded_at=c.source_uploaded_at,
                added_to_syllabus=c.added_to_syllabus,
            )
            for c in run.changes
        ],
    )


def _zone(tz: Optional[str]) -> ZoneInfo:
    try:
        return ZoneInfo(tz or "UTC")
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"unknown time zone: {tz}") from exc


@router.get("/status", response_model=SyncStatusResponse)
async def get_sync_status(
    user: UserDep,
    rt: RuntimeDep,
    tz: Optional[str] = Query(
        default=None,
        description="The user's IANA time zone (e.g. Europe/London) for next_expected_at; "
        "default UTC",
    ),
) -> SyncStatusResponse:
    zone = _zone(tz)
    runs = await SyncRunService(rt).recent(user.user_id, 1)
    last = runs[0] if runs else None
    now = datetime.now(timezone.utc)
    schedule = parse_schedule_time(rt.settings.sync_schedule_time)
    return SyncStatusResponse(
        last_run=run_response(last) if last else None,
        next_expected_at=next_expected(now, schedule, zone),
        health=sync_health(
            last.started_at if last else None,
            now,
            timedelta(hours=rt.settings.sync_late_after_hours),
        ),
    )


@router.get("/runs", response_model=list[SyncRunResponse])
async def list_sync_runs(
    user: UserDep,
    rt: RuntimeDep,
    limit: int = Query(default=10, ge=1, le=100),
) -> list[SyncRunResponse]:
    runs = await SyncRunService(rt).recent(user.user_id, limit)
    return [run_response(r) for r in runs]


@router.get("/prompt", response_model=SyncPromptResponse)
async def get_sync_prompt(user: UserDep, rt: RuntimeDep) -> SyncPromptResponse:
    prompt = load_prompt()
    return SyncPromptResponse(
        prompt=prompt.prompt,
        schedule_time=rt.settings.sync_schedule_time,
        version=prompt.version,
    )
