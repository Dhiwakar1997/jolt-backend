"""MCP sync tools: get_unprocessed_sources, get_pending_gradings,
submit_gradings, sync (LLD §7.2).

`submit_gradings` recomputes FSRS inline (single-service design) — no job.
"""

from __future__ import annotations

from jolt.domain.grading import GradingInput
from jolt.mcp.context import get_runtime, require_user
from jolt.services.sync import SyncService


async def sync(limit: int = 50) -> dict:
    """Return the sync plan: how many sources and gradings are waiting."""
    user = await require_user()
    plan = await SyncService(get_runtime()).plan(user.user_id, limit=limit)
    return {
        "unprocessed_sources": plan.unprocessed_sources,
        "pending_gradings": plan.pending_gradings,
    }


async def get_unprocessed_sources(limit: int = 10) -> dict:
    user = await require_user()
    sources = await SyncService(get_runtime()).get_unprocessed_sources(user.user_id, limit)
    return {
        "sources": [
            {
                "source_id": s.id,
                "filename": s.filename,
                "content_type": s.content_type,
                "lease_expires_at": s.lease_expires_at.isoformat()
                if s.lease_expires_at
                else None,
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
    inputs = [
        GradingInput(
            review_id=g["review_id"],
            suggested_fsrs_grade=g["suggested_fsrs_grade"],
            rationale=g.get("rationale"),
            rubric_version=g.get("rubric_version"),
        )
        for g in gradings
    ]
    applied = await SyncService(get_runtime()).submit_gradings(user.user_id, inputs)
    return {"applied": applied, "submitted": len(inputs)}
