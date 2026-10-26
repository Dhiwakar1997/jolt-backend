"""MCP sync tools: get_unprocessed_sources, get_pending_gradings,
submit_gradings, sync (LLD §7.2).

`submit_gradings` recomputes FSRS inline (single-service design) — no job — and
stores rewritten questions as new versions when the user allows it (BE-06).
"""

from __future__ import annotations

from jolt.domain.grading import GradingInput, QuestionRewrite
from jolt.mcp.context import get_runtime, require_user
from jolt.services.gradings import GradingService
from jolt.services.reviews import ReviewService
from jolt.services.sync import SyncService


async def sync(limit: int = 50) -> dict:
    """Return the sync plan: how many sources and gradings are waiting."""
    user = await require_user()
    plan = await SyncService(get_runtime()).plan(user.user_id, limit=limit)
    return {
        "unprocessed_sources": plan.unprocessed_sources,
        "pending_gradings": plan.pending_gradings,
        "rereads": [
            {
                "source_id": r.source_id,
                "title": r.title,
                "preferred_model": r.preferred_model,
                "requested_at": r.requested_at.isoformat(),
            }
            for r in plan.rereads
        ],
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
                "lease_expires_at": s.lease_expires_at.isoformat() if s.lease_expires_at else None,
                "title": s.title or s.filename,
                "track_id": s.track_id,
                "page_count": len(s.page_list()),
                # Read again (BE-05): store this read with supersede=true.
                "supersede": s.active_extraction_id is not None,
                "preferred_model": s.reread_preferred_model,
            }
            for s in sources
        ]
    }


async def get_pending_gradings(limit: int = 10) -> dict:
    user = await require_user()
    rt = get_runtime()
    pending = await SyncService(rt).get_pending_gradings(user.user_id, limit)
    settings = await ReviewService(rt).user_settings(user.user_id)
    return {
        "gradings": [
            {
                "review_id": p.review_id,
                "concept_id": p.concept_id,
                "question_id": p.question_id,
                "free_text": p.free_text,
                "expected_answer": p.expected_answer,
                "stem": p.stem,
                "options": p.options,
                "correct_index": p.correct_index,
                "question_version": p.question_version,
                "selected_index": p.selected_index,
                "is_correct": p.is_correct,
            }
            for p in pending
        ],
        # BE-06 (decision D8): rewrites are stored only when this is true.
        "allow_question_rewrite": settings.allow_question_rewrite,
    }


def _rewrite(raw) -> QuestionRewrite | None:
    if not isinstance(raw, dict):
        return None
    return QuestionRewrite(
        stem=raw.get("stem", ""),
        options=raw.get("options", []),
        correct_index=raw.get("correct_index", -1),
        expected_answer=raw.get("expected_answer", ""),
        rewrite_reason=raw.get("rewrite_reason"),
    )


async def submit_gradings(gradings: list[dict]) -> dict:
    user = await require_user()
    inputs = [
        GradingInput(
            review_id=g["review_id"],
            suggested_fsrs_grade=g["suggested_fsrs_grade"],
            rationale=g.get("rationale"),
            rubric_version=g.get("rubric_version"),
            rewritten_question=_rewrite(g.get("rewritten_question")),
        )
        for g in gradings
    ]
    outcome = await GradingService(get_runtime()).submit(user.user_id, inputs)
    return {
        "applied": outcome.applied,
        "submitted": outcome.submitted,
        "rewritten": [
            {
                "review_id": r.review_id,
                "question_id": r.question_id,
                "version": r.version,
                "previous_version_id": r.previous_version_id,
            }
            for r in outcome.rewritten
        ],
        "rewrites_skipped": [
            {"review_id": r.review_id, "reason": r.reason} for r in outcome.rewrites_skipped
        ],
    }
