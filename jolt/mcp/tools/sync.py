"""MCP sync tools: sync, get_unprocessed_sources, get_pending_gradings,
submit_gradings (LLD §7.2, BE-04).

`jolt_sync` opens (or continues) the caller's sync run and returns the plan;
the other tools add what they did to that run. `submit_gradings` recomputes
FSRS inline (single-service design) — no job.
"""

from __future__ import annotations

from typing import Optional

from jolt.domain.grading import GradingInput
from jolt.domain.models import SyncRun, SyncRunStatus
from jolt.domain.sync import SyncPlan
from jolt.mcp.context import get_runtime, require_user
from jolt.services.sync import SyncService
from jolt.services.sync_runs import SyncRunService, add_error


def _iso(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


def plan_dict(plan: SyncPlan) -> dict:
    return {
        "run_id": plan.run_id,
        "done": plan.done,
        "unprocessed_sources": plan.unprocessed_sources,
        "pending_gradings": plan.pending_gradings,
        "rereads": plan.rereads,
        "claimed_sources": [
            {
                "source_id": s.source_id,
                "filename": s.filename,
                "track_id": s.track_id,
                "uploaded_at": s.uploaded_at.isoformat(),
                "lease_expires_at": _iso(s.lease_expires_at),
            }
            for s in plan.claimed_sources
        ],
        "in_flight_elsewhere": plan.in_flight_elsewhere,
        "unassigned_sources": plan.unassigned_sources,
        "upload_dates": plan.upload_dates,
        "tracks": [
            {
                "track_id": t.track_id,
                "name": t.name,
                "agenda_status": t.agenda_status,
                "unprocessed_sources": t.unprocessed_sources,
                "pending_gradings": t.pending_gradings,
                "rereads": t.rereads,
                "guidance": t.guidance,
            }
            for t in plan.tracks
        ],
        "reread_sources": [
            {
                "source_id": r.source_id,
                "filename": r.filename,
                "track_id": r.track_id,
                "preferred_model": r.preferred_model,
                "requested_at": _iso(r.requested_at),
            }
            for r in plan.reread_sources
        ],
        "steps": [
            {
                "step": st.step,
                "tool": st.tool,
                "purpose": st.purpose,
                "limit": st.limit,
                "repeat": st.repeat,
            }
            for st in plan.steps
        ],
        "limits": {
            "sources_per_claim": plan.limits.sources_per_claim,
            "gradings_per_claim": plan.limits.gradings_per_claim,
            "gradings_per_submit": plan.limits.gradings_per_submit,
            "concepts_per_session": plan.limits.concepts_per_session,
            "questions_per_concept_min": plan.limits.questions_per_concept_min,
            "questions_per_concept_max": plan.limits.questions_per_concept_max,
            "options_per_question": plan.limits.options_per_question,
            "lease_seconds": plan.limits.lease_seconds,
        },
        "next": plan.next,
    }


async def sync(limit: int = 50, errors: Optional[list[str]] = None) -> dict:
    """Open or continue the caller's sync run and return the plan."""
    user = await require_user()
    rt = get_runtime()
    runs = SyncRunService(rt)
    await runs.close_stale(user.user_id)
    current = await runs.current(user)  # running, or finished a moment ago
    plan = await SyncService(rt).plan(
        user.user_id,
        claimed_ids=set(current.claimed_source_ids) if current else set(),
        limit=limit,
    )
    if current is None or (current.status != SyncRunStatus.RUNNING and not plan.done):
        run = await runs.start(user)  # new work since the last run closed
    else:
        run = current

    def note(r: SyncRun) -> None:
        r.plan_calls += 1
        for message in errors or []:
            if str(message).strip():
                add_error(r, str(message))

    run = await runs.update(run, note)
    if plan.done and run.status == SyncRunStatus.RUNNING:
        run = await runs.finish(run)
    plan.run_id = run.id
    return plan_dict(plan)


async def get_unprocessed_sources(limit: int = 10) -> dict:
    user = await require_user()
    rt = get_runtime()
    sources = await SyncService(rt).get_unprocessed_sources(user.user_id, limit)
    if sources:

        def claimed(r: SyncRun) -> None:
            r.claimed_source_ids.extend(s.id for s in sources if s.id not in r.claimed_source_ids)

        await SyncRunService(rt).record(user, claimed)
    return {
        "sources": [
            {
                "source_id": s.id,
                "filename": s.filename,
                "content_type": s.content_type,
                "lease_expires_at": s.lease_expires_at.isoformat() if s.lease_expires_at else None,
                "uploaded_at": s.created_at.isoformat(),
            }
            for s in sources
        ]
    }


async def get_pending_gradings(limit: int = 10) -> dict:
    user = await require_user()
    pending = await SyncService(get_runtime()).get_pending_gradings(user.user_id, limit)
    return {
        "gradings": [
            {
                "review_id": p.review_id,
                "concept_id": p.concept_id,
                "question_id": p.question_id,
                "free_text": p.free_text,
                "expected_answer": p.expected_answer,
            }
            for p in pending
        ]
    }


async def submit_gradings(gradings: list[dict]) -> dict:
    user = await require_user()
    rt = get_runtime()
    inputs = [
        GradingInput(
            review_id=g["review_id"],
            suggested_fsrs_grade=g["suggested_fsrs_grade"],
            rationale=g.get("rationale"),
            rubric_version=g.get("rubric_version"),
        )
        for g in gradings
    ]
    applied = await SyncService(rt).submit_gradings(user.user_id, inputs)
    if applied:

        def graded(r: SyncRun) -> None:
            r.gradings_submitted += applied

        await SyncRunService(rt).record(user, graded)
    return {"applied": applied, "submitted": len(inputs)}
