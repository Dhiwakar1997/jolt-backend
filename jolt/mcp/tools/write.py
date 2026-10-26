"""MCP write tools: request_upload_url, log_session, store_extraction,
create_questions, correct_concept, set_agenda (LLD §4, §7.2, §7.7).

Every concept-, question- and track-keyed write checks that the row is the
caller's (BE-07, decision A16): see `jolt.services.ownership`.
"""

from __future__ import annotations

from jolt.domain.models import AgendaSource, SyllabusItem, SyncChange, SyncChangeKind, SyncRun
from jolt.mcp.context import get_runtime, require_user
from jolt.mcp.tools.read import agenda_dict
from jolt.services.corrections import CorrectionService
from jolt.services.ownership import NotOwned
from jolt.services.reconcile import ReconcileError, ReconcileService, ReconcileView
from jolt.services.sync import ConceptInput, QuestionInput, SyncError, SyncService
from jolt.services.sync_runs import SyncRunService, add_change, concept_title
from jolt.services.tracks import AgendaLocked, TrackError, TrackService
from jolt.services.uploads import PageSpec, UploadInvalid, UploadService


async def request_upload_url(filename: str, content_type: str, sha256: str) -> dict:
    user = await require_user()
    try:
        ticket = await UploadService(get_runtime()).request_upload(
            user.user_id,
            [PageSpec(filename=filename, content_type=content_type, sha256=sha256)],
            loc_prefix=(),
        )
    except UploadInvalid as exc:
        return {"error": exc.msg}
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
            page=c.get("page"),
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
                    concept_title=concept_title(concept),
                    track_id=concept.track_id,
                    source_id=source.id if source else None,
                    source_uploaded_at=source.uploaded_at if source else None,
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


def reconciliation_dict(view: ReconcileView) -> dict:
    return {
        "status": view.status,
        "total_steps": view.total_steps,
        "applied_steps": view.applied_steps,
        "remaining_steps": view.remaining_steps,
        "max_steps_per_call": view.max_steps_per_call,
        "concept_ids": view.concept_ids,
        "deltas": [
            {
                "index": d.index,
                "change": d.change,
                "concept_id": d.concept_id,
                "existing_concept_id": d.existing_concept_id,
                "similarity": d.similarity,
                "applied": d.applied,
            }
            for d in view.deltas
        ],
    }


# Reconciliation classes that show up in "New since last sync" (BE-04 changes).
_RECONCILE_CHANGE_KIND = {
    "new": SyncChangeKind.CONCEPT_ADDED,
    "changed": SyncChangeKind.CONCEPT_ADDED,  # the successor concept
    "refined": SyncChangeKind.CONCEPT_UPDATED,
}


async def record_reconciliation(rt, user, source_id: str, view: ReconcileView) -> None:
    """Add a reconciliation's applied steps to the caller's sync run (BE-07).

    New and successor concepts are `concept_added`, refined ones
    `concept_updated`. A step already on the run (an earlier partial call) is not
    added twice. Best effort, like all run bookkeeping.
    """
    try:
        wanted = [
            (d.concept_id, _RECONCILE_CHANGE_KIND[d.change])
            for d in view.deltas
            if d.applied and d.concept_id and d.change in _RECONCILE_CHANGE_KIND
        ]
        if not wanted:
            return
        source = await rt.repos.sources.get_source(source_id, user.user_id)
        changes: list[SyncChange] = []
        for concept_id, kind in wanted:
            state = await rt.repos.concept_states.for_concept(user.user_id, concept_id)
            if state is None or not state.track_id:
                continue
            concept = await rt.repos.concepts.get_concept(concept_id, state.track_id)
            if concept is None:
                continue
            changes.append(
                SyncChange(
                    kind=kind,
                    concept_id=concept.id,
                    concept_title=concept_title(concept),
                    track_id=concept.track_id,
                    source_id=source.id if source else None,
                    source_uploaded_at=source.uploaded_at if source else None,
                )
            )

        def reconciled(r: SyncRun) -> None:
            seen = {(c.kind, c.concept_id) for c in r.changes}
            for change in changes:
                if (change.kind, change.concept_id) not in seen:
                    add_change(r, change)

        await SyncRunService(rt).record(user, reconciled)
    except Exception:  # never fail the tool call over bookkeeping
        return


async def store_extraction(
    source_id: str,
    markdown: str,
    confidence: float = 0.0,
    model_id: str | None = None,
    supersede: bool = False,
    concepts: list[dict] | None = None,
) -> dict:
    user = await require_user()
    rt = get_runtime()
    reconcile = ReconcileService(rt)
    proposed = None
    if concepts is not None:
        # Validate before anything is written: a bad concept stores nothing.
        source = await rt.repos.sources.get_source(source_id, user.user_id)
        if source is None:
            raise SyncError("source not found")
        try:
            proposed = await reconcile.prepare(user.user_id, source, concepts)
        except ReconcileError as exc:
            return {"error": str(exc)}
        except NotOwned as exc:
            raise SyncError(str(exc)) from exc
    # The connected client's name for "Past reads" (the MCP token's label).
    extraction = await SyncService(rt).store_extraction(
        user.user_id,
        source_id,
        markdown,
        confidence,
        model_id,
        supersede=supersede,
        client_label=user.token_label,
    )

    def processed(r: SyncRun) -> None:
        if source_id not in r.processed_source_ids:
            r.processed_source_ids.append(source_id)

    await SyncRunService(rt).record(user, processed)
    result: dict = {
        "extraction_id": extraction.id,
        "status": extraction.status.value,
        "reconciliation": None,
    }
    if proposed is not None:
        source = await rt.repos.sources.get_source(source_id, user.user_id)
        view = await reconcile.start(user.user_id, source, extraction, proposed)
        result["reconciliation"] = reconciliation_dict(view)
        await record_reconciliation(rt, user, source_id, view)
    return result


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
    """Apply a correction to the caller's concept text in place; clears its notes."""
    user = await require_user()
    rt = get_runtime()
    try:
        result = await CorrectionService(rt).correct(user.user_id, track_id, concept_id, text)
    except NotOwned:
        return {"error": "concept not found"}
    if result.text_changed and result.concept is not None:
        # "New since last sync" (BE-04): a rewritten concept is a concept_updated change.
        concept = result.concept
        source = (
            await rt.repos.sources.get_source(concept.source_id, user.user_id)
            if concept.source_id
            else None
        )
        change = SyncChange(
            kind=SyncChangeKind.CONCEPT_UPDATED,
            concept_id=concept.id,
            concept_title=concept_title(concept),
            track_id=concept.track_id,
            source_id=source.id if source else None,
            source_uploaded_at=source.uploaded_at if source else None,
        )
        await SyncRunService(rt).record(user, lambda r: add_change(r, change))
    return {
        "concept_id": result.concept_id,
        "updated": result.updated,
        "corrections_cleared": result.corrections_cleared,
    }


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
        item_source = entry.get("source")
        if item_source is not None and item_source not in {s.value for s in AgendaSource}:
            return {"error": f"invalid item source '{item_source}'; expected agent|user|emergent"}
        items.append(
            SyllabusItem(
                concept_key=entry["concept_key"],
                label=entry["label"],
                parent=entry.get("parent"),
                source=AgendaSource(item_source) if item_source else None,
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
    return agenda_dict(agenda)
