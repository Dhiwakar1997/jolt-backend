"""MCP extraction tools: list_sources, get_source_content, diff_extractions,
store_extraction(supersede) (LLD §4, §7.7).

`diff_extractions` runs the pure reconciliation classifier (domain/reconciliation)
over an agent-supplied set of proposed concepts vs. the caller's active concepts
on the track, so the agent can preview a supersede before committing it.
`continue_reconciliation` resumes a large supersede from its checkpoint (BE-07).
"""

from __future__ import annotations

from jolt.domain.reconciliation import ProposedConcept, reconcile
from jolt.mcp.context import get_runtime, require_user
from jolt.mcp.tools.write import reconciliation_dict, record_reconciliation
from jolt.services.ownership import NotOwned, visible_concepts
from jolt.services.reconcile import ReconcileError, ReconcileService
from jolt.services.sync import SyncService


async def list_sources() -> dict:
    user = await require_user()
    sources = await get_runtime().repos.sources.list_for_user(user.user_id)
    return {
        "sources": [
            {
                "source_id": s.id,
                "filename": s.filename,
                "processing_status": s.processing_status.value,
                "active_extraction_id": s.active_extraction_id,
                "page_count": len(s.page_list()),
                "reread_requested": s.reread_requested_at is not None,
            }
            for s in sources
            if s.confirmed_at is not None
        ]
    }


async def get_source_content(source_id: str) -> dict:
    user = await require_user()
    content = await SyncService(get_runtime()).get_source_content(user.user_id, source_id)
    return {
        "source_id": content.source_id,
        "filename": content.filename,
        "content_type": content.content_type,
        "read_url": content.read_url,
        "page_count": len(content.pages),
        "pages": [
            {
                "index": p.index,
                "filename": p.filename,
                "content_type": p.content_type,
                "read_url": p.read_url,
            }
            for p in content.pages
        ],
    }


async def diff_extractions(track_id: str, proposed_concepts: list[dict]) -> dict:
    """Preview the per-concept classification a supersede would produce."""
    user = await require_user()
    try:
        # The caller's concepts on a visible track only (BE-07, A16).
        existing, _ = await visible_concepts(get_runtime(), user.user_id, track_id)
    except NotOwned as exc:
        raise ValueError(str(exc)) from exc
    proposed = [
        ProposedConcept(text=p["text"], title=p.get("title"), syllabus_ref=p.get("syllabus_ref"))
        for p in proposed_concepts
    ]
    result = reconcile(existing, proposed)
    return {
        "deltas": [
            {
                "change": d.change.value,
                "existing_concept_id": d.existing.id if d.existing else None,
                "proposed_text": d.proposed.text if d.proposed else None,
                "similarity": round(d.similarity, 3),
            }
            for d in result.deltas
        ]
    }


async def continue_reconciliation(source_id: str) -> dict:
    """Resume a supersede reconciliation from its checkpoint (BE-07 §6)."""
    user = await require_user()
    rt = get_runtime()
    try:
        view = await ReconcileService(rt).resume(user.user_id, source_id)
    except ReconcileError as exc:
        return {"error": str(exc)}
    except NotOwned as exc:
        raise ValueError(str(exc)) from exc
    await record_reconciliation(rt, user, source_id, view)
    return reconciliation_dict(view)
