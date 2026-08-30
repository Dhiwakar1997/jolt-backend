"""MCP extraction tools: list_sources, get_source_content, diff_extractions,
store_extraction(supersede) (LLD §4, §7.7).

`diff_extractions` runs the pure reconciliation classifier (domain/reconciliation)
over an agent-supplied set of proposed concepts vs. the active concepts, so the
agent can preview a supersede before committing it.
"""

from __future__ import annotations

from jolt.domain.reconciliation import ProposedConcept, reconcile
from jolt.mcp.context import get_runtime, require_user
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
            }
            for s in sources
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
    }


async def diff_extractions(track_id: str, proposed_concepts: list[dict]) -> dict:
    """Preview the per-concept classification a supersede would produce."""
    user = await require_user()
    rt = get_runtime()
    existing = await rt.repos.concepts.list_for_track(track_id)
    proposed = [
        ProposedConcept(
            text=p["text"], title=p.get("title"), syllabus_ref=p.get("syllabus_ref")
        )
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
