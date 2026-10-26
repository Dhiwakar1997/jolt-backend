"""MCP write tools: request_upload_url, log_session, store_extraction,
create_questions, correct_concept (LLD §4, §7.2, §7.7).
"""

from __future__ import annotations

from jolt.domain.models import (
    AgendaSource,
    ConceptStatus,
    SyllabusItem,
    SyncChange,
    SyncChangeKind,
    SyncRun,
)
from jolt.mcp.context import get_runtime, require_user
from jolt.services.sync import ConceptInput, QuestionInput, SyncService
from jolt.services.sync_runs import SyncRunService, add_change
from jolt.services.tracks import AgendaLocked, TrackError, TrackService
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
            concept_key=c.get("concept_key"),
            parent=c.get("parent"),
        )
        for c in concepts
    ]
    rt = get_runtime()
    runs = SyncRunService(rt)
    run = await runs.current(user)
    logged = await SyncService(rt).log_session(
        user.user_id, source_ids, inputs, sync_run_id=run.id if run else None
    )
    if run is not None:
        # One source in the call: concepts without their own source_id came from it.
        only_source = source_ids[0] if len(set(source_ids)) == 1 else None
        changes = []
        for c_in, concept in zip(inputs, logged.concepts):
            source_id = concept.source_id or only_source
            source = logged.sources.get(source_id) if source_id else None
            key = c_in.effective_key()
            in_agenda = bool(key) and (concept.track_id, key) in logged.added_to_agenda
            changes.append(
                SyncChange(
                    kind=SyncChangeKind.CONCEPT_ADDED,
                    concept_id=concept.id,
                    concept_title=_concept_title(concept),
                    track_id=concept.track_id,
                    source_id=source.id if source else None,
                    source_uploaded_at=source.created_at if source else None,
                    added_to_syllabus=in_agenda,
                )
            )

        def added(r: SyncRun) -> None:
            for change in changes:
                add_change(r, change)

        await runs.update(run, added)
    return {
        "session_id": logged.sessions[0].id,
        "session_ids": [s.id for s in logged.sessions],
        "concept_ids": [c.id for c in logged.concepts],
    }


def _concept_title(concept) -> str:
    if concept.title:
        return concept.title
    text = " ".join(concept.text.split())
    return text if len(text) <= 80 else text[:79].rstrip() + "…"


async def store_extraction(
    source_id: str,
    markdown: str,
    confidence: float = 0.0,
    model_id: str | None = None,
    supersede: bool = False,
) -> dict:
    user = await require_user()
    rt = get_runtime()
    extraction = await SyncService(rt).store_extraction(
        user.user_id, source_id, markdown, confidence, model_id, supersede=supersede
    )

    def processed(r: SyncRun) -> None:
        if source_id not in r.processed_source_ids:
            r.processed_source_ids.append(source_id)

    await SyncRunService(rt).record(user, processed)
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
    rt = get_runtime()
    created = await SyncService(rt).create_questions(user.user_id, concept_id, inputs)
    if created:

        def written(r: SyncRun) -> None:
            r.questions_written += len(created)

        await SyncRunService(rt).record(user, written)
    return {"question_ids": [q.id for q in created]}


async def correct_concept(track_id: str, concept_id: str, text: str) -> dict:
    """Apply an agent-supplied correction to a concept's text in place."""
    # Auth gate only: concepts are track-partitioned, not user-scoped (see docs/GAPS.md).
    user = await require_user()
    rt = get_runtime()
    concept = await rt.repos.concepts.get_concept(concept_id, track_id)
    if concept is None:
        return {"error": "concept not found"}
    concept.text = text
    concept.status = ConceptStatus.ACTIVE
    updated = await rt.repos.concepts.replace(concept, etag=concept.etag)
    if updated is not None:
        source = (
            await rt.repos.sources.get_source(updated.source_id, user.user_id)
            if updated.source_id
            else None
        )
        change = SyncChange(
            kind=SyncChangeKind.CONCEPT_UPDATED,
            concept_id=concept_id,
            concept_title=_concept_title(updated),
            track_id=track_id,
            source_id=source.id if source else None,
            source_uploaded_at=source.created_at if source else None,
        )
        await SyncRunService(rt).record(user, lambda r: add_change(r, change))
    return {"concept_id": concept_id, "updated": updated is not None}


async def set_agenda(
    track_id: str,
    syllabus: list[dict],
    source: str = "agent",
    mark_refined: bool = False,
) -> dict:
    """Write/replace a track's agenda syllabus. Rejected on a locked agenda."""
    user = await require_user()
    try:
        agenda_source = AgendaSource(source)
    except ValueError:
        return {"error": f"invalid source '{source}'; expected agent|user|emergent"}

    items: list[SyllabusItem] = []
    for entry in syllabus:
        if not entry.get("concept_key") or not entry.get("label"):
            return {"error": "each syllabus item needs concept_key and label"}
        items.append(
            SyllabusItem(
                concept_key=entry["concept_key"],
                label=entry["label"],
                parent=entry.get("parent"),
            )
        )

    try:
        agenda = await TrackService(get_runtime()).set_agenda(
            user.user_id,
            track_id,
            items,
            agenda_source,
            mark_refined=mark_refined,
        )
    except AgendaLocked:
        return {"error": "agenda is locked; refinement is frozen"}
    except TrackError as exc:
        return {"error": str(exc)}
    return _agenda_dict(agenda)


def _agenda_dict(agenda) -> dict:
    return {
        "status": agenda.status.value,
        "source": agenda.source.value,
        "syllabus": [
            {"concept_key": i.concept_key, "label": i.label, "parent": i.parent}
            for i in agenda.syllabus
        ],
        "last_refined_at": (agenda.last_refined_at.isoformat() if agenda.last_refined_at else None),
        "refined_from_source_id": agenda.refined_from_source_id,
    }
