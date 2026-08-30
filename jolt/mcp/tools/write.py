"""MCP write tools: request_upload_url, log_session, store_extraction,
create_questions, correct_concept (LLD §4, §7.2, §7.7).
"""

from __future__ import annotations

from jolt.domain.models import ConceptStatus
from jolt.mcp.context import get_runtime, require_user
from jolt.services.sync import ConceptInput, QuestionInput, SyncService
from jolt.services.uploads import UploadService


async def request_upload_url(filename: str, content_type: str, sha256: str) -> dict:
    user = await require_user()
    ticket = await UploadService(get_runtime()).request_upload(
        user.user_id, filename, content_type, sha256
    )
    return {"source_id": ticket.source_id, "upload_url": ticket.upload_url}


async def log_session(source_ids: list[str], concepts: list[dict]) -> dict:
    user = await require_user()
    inputs = [
        ConceptInput(
            text=c["text"],
            track_id=c["track_id"],
            title=c.get("title"),
            syllabus_ref=c.get("syllabus_ref"),
            source_id=c.get("source_id"),
        )
        for c in concepts
    ]
    session, created = await SyncService(get_runtime()).log_session(
        user.user_id, source_ids, inputs
    )
    return {
        "session_id": session.id,
        "concept_ids": [c.id for c in created],
    }


async def store_extraction(
    source_id: str,
    markdown: str,
    confidence: float = 0.0,
    model_id: str | None = None,
    supersede: bool = False,
) -> dict:
    user = await require_user()
    extraction = await SyncService(get_runtime()).store_extraction(
        user.user_id, source_id, markdown, confidence, model_id, supersede=supersede
    )
    return {"extraction_id": extraction.id, "status": extraction.status.value}


async def create_questions(concept_id: str, questions: list[dict]) -> dict:
    user = await require_user()
    inputs = [
        QuestionInput(
            stem=q["stem"],
            options=q["options"],
            correct_index=q["correct_index"],
            expected_answer=q["expected_answer"],
        )
        for q in questions
    ]
    created = await SyncService(get_runtime()).create_questions(
        user.user_id, concept_id, inputs
    )
    return {"question_ids": [q.id for q in created]}


async def correct_concept(track_id: str, concept_id: str, text: str) -> dict:
    """Apply an agent-supplied correction to a concept's text in place."""
    user = await require_user()
    rt = get_runtime()
    concept = await rt.repos.concepts.get_concept(concept_id, track_id)
    if concept is None:
        return {"error": "concept not found"}
    concept.text = text
    concept.status = ConceptStatus.ACTIVE
    updated = await rt.repos.concepts.replace(concept, etag=concept.etag)
    return {"concept_id": concept_id, "updated": updated is not None}
