"""Output schemas for every MCP tool — the result half of the agent contract.

FastMCP derives each tool's *input* schema from its Python signature, but the
tools return plain dicts, so the advertised output type is just "object". These
Pydantic models pin the exact result shape of each tool. They are:

* exported into `contract/mcp-tools.json` by `scripts/export_contract.py`, and
* checked against real tool results in `tests/test_api_smoke.py`, so a tool
  whose dict drifts from its model fails the build.

Tools that report a recoverable problem return `{ "error": "<message>" }`
instead of raising; their schema is a union with `ToolError`. Every other
failure (bad token, unknown source, ...) is raised and reaches the agent as an
MCP tool error (`isError: true`), which has no output schema.
"""

from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, TypeAdapter


class _Out(BaseModel):
    # Results never carry extra keys; forbid them so drift fails the tests.
    model_config = ConfigDict(extra="forbid")


class ToolError(_Out):
    error: str


# -- agenda / tracks -------------------------------------------------------
class SyllabusItemOut(_Out):
    concept_key: str
    label: str
    parent: Optional[str]
    # BE-07: per-item provenance (falls back to the agenda's source).
    source: Literal["agent", "user", "emergent"]


class AgendaOut(_Out):
    status: Literal["none", "draft", "refined", "locked"]
    source: Literal["agent", "user", "emergent"]
    syllabus: list[SyllabusItemOut]
    last_refined_at: Optional[str]  # ISO-8601 UTC
    refined_from_source_id: Optional[str]
    # BE-07: the learner's outline from track creation (a `draft` to tidy), or null.
    outline: Optional[str]


class TrackOut(_Out):
    id: str
    name: str
    origin: Literal["user", "jolt"]
    kind: Literal["notes", "dsa_patterns"]
    archived: bool
    syllabus: list[str]
    agenda: AgendaOut


class GetTracksOut(_Out):
    tracks: list[TrackOut]


class RecentConceptOut(_Out):
    id: str
    text: str
    title: Optional[str]
    syllabus_ref: Optional[str]


class GetRecentConceptsOut(_Out):
    concepts: list[RecentConceptOut]


class GetCoverageOut(_Out):
    track_id: str
    total_concepts: int
    studied_concepts: int
    decaying_concepts: int
    off_plan_concepts: int  # BE-07: on a locked track, outside the syllabus; not counted
    coverage_ratio: float
    uncovered_syllabus: list[str]


class CorrectionNoteOut(_Out):
    note: str
    created_at: str  # ISO-8601 UTC


class PendingCorrectionOut(_Out):
    concept_id: str
    track_id: str
    title: str
    text: str
    notes: list[CorrectionNoteOut]


class GetPendingCorrectionsOut(_Out):
    corrections: list[PendingCorrectionOut]


# -- writes ----------------------------------------------------------------
class RequestUploadUrlOut(_Out):
    source_id: str
    upload_url: str


class LogSessionOut(_Out):
    session_id: str
    concept_ids: list[str]


class ReconcileDeltaOut(_Out):
    index: Optional[int]  # position in `concepts`; null for a removed concept
    change: Literal["unchanged", "refined", "changed", "removed", "new"]
    concept_id: Optional[str]  # the concept that carries on; null when removed
    existing_concept_id: Optional[str]
    similarity: float
    applied: bool


class ReconciliationOut(_Out):
    status: Literal["complete", "partial"]
    total_steps: int
    applied_steps: int
    remaining_steps: int
    max_steps_per_call: int
    concept_ids: list[Optional[str]]  # input order
    deltas: list[ReconcileDeltaOut]


class StoreExtractionOut(_Out):
    extraction_id: str
    status: Literal["active", "superseded"]
    # BE-07: present when the call passed `concepts`, else null.
    reconciliation: Optional[ReconciliationOut]


class CreateQuestionsOut(_Out):
    question_ids: list[str]


class CorrectConceptOut(_Out):
    concept_id: str
    updated: bool
    corrections_cleared: int  # BE-07: the learner's notes this answered


# -- sync ------------------------------------------------------------------
class RereadOut(_Out):
    source_id: str
    title: str
    preferred_model: Optional[str]
    requested_at: str  # ISO-8601 UTC


class SyncOut(_Out):
    unprocessed_sources: int
    pending_gradings: int
    pending_corrections: int  # BE-07: concepts with correction notes waiting
    rereads: list[RereadOut]


class UnprocessedSourceOut(_Out):
    source_id: str
    filename: str
    content_type: str
    lease_expires_at: Optional[str]  # ISO-8601 UTC
    title: str
    track_id: Optional[str]
    page_count: int
    supersede: bool
    preferred_model: Optional[str]


class GetUnprocessedSourcesOut(_Out):
    sources: list[UnprocessedSourceOut]


class PendingGradingOut(_Out):
    review_id: str
    concept_id: str
    question_id: str
    free_text: str
    expected_answer: str
    # BE-06: the question as answered (for a rewrite) and the learner's choice.
    stem: str
    options: list[str]
    correct_index: int
    question_version: int
    selected_index: Optional[int]
    is_correct: Optional[bool]


class GetPendingGradingsOut(_Out):
    gradings: list[PendingGradingOut]
    allow_question_rewrite: bool


class RewrittenQuestionOut(_Out):
    review_id: str
    question_id: str  # the new version
    version: int
    previous_version_id: Optional[str]


class RewriteSkippedOut(_Out):
    review_id: str
    reason: str


class SubmitGradingsOut(_Out):
    applied: int
    submitted: int
    rewritten: list[RewrittenQuestionOut]
    rewrites_skipped: list[RewriteSkippedOut]


# -- sources / extraction --------------------------------------------------
class ListedSourceOut(_Out):
    source_id: str
    filename: str
    processing_status: Literal["unprocessed", "in_flight", "processed", "failed"]
    active_extraction_id: Optional[str]
    page_count: int
    reread_requested: bool


class ListSourcesOut(_Out):
    sources: list[ListedSourceOut]


class SourcePageOut(_Out):
    index: int
    filename: str
    content_type: str
    read_url: str


class GetSourceContentOut(_Out):
    source_id: str
    filename: str
    content_type: str
    read_url: str
    page_count: int
    pages: list[SourcePageOut]


class ExtractionDeltaOut(_Out):
    change: Literal["unchanged", "refined", "changed", "removed", "new"]
    existing_concept_id: Optional[str]
    proposed_text: Optional[str]
    similarity: float


class DiffExtractionsOut(_Out):
    deltas: list[ExtractionDeltaOut]


# Tool name -> result type. Every tool in jolt.mcp.server._TOOLS must appear.
TOOL_OUTPUTS: dict[str, Any] = {
    "jolt_get_tracks": GetTracksOut,
    "jolt_get_recent_concepts": GetRecentConceptsOut,
    "jolt_get_coverage": GetCoverageOut,
    "jolt_get_agenda": Union[AgendaOut, ToolError],
    "jolt_get_pending_corrections": GetPendingCorrectionsOut,
    "jolt_request_upload_url": Union[RequestUploadUrlOut, ToolError],
    "jolt_log_session": LogSessionOut,
    "jolt_set_agenda": Union[AgendaOut, ToolError],
    "jolt_store_extraction": Union[StoreExtractionOut, ToolError],
    "jolt_create_questions": CreateQuestionsOut,
    "jolt_correct_concept": Union[CorrectConceptOut, ToolError],
    "jolt_sync": SyncOut,
    "jolt_get_unprocessed_sources": GetUnprocessedSourcesOut,
    "jolt_get_pending_gradings": GetPendingGradingsOut,
    "jolt_submit_gradings": SubmitGradingsOut,
    "jolt_list_sources": ListSourcesOut,
    "jolt_get_source_content": GetSourceContentOut,
    "jolt_diff_extractions": DiffExtractionsOut,
    "jolt_continue_reconciliation": Union[ReconciliationOut, ToolError],
}


def output_adapter(tool_name: str) -> TypeAdapter:
    return TypeAdapter(TOOL_OUTPUTS[tool_name])


def output_schema(tool_name: str) -> dict:
    """JSON Schema for a tool's result."""
    return output_adapter(tool_name).json_schema()


def validate_output(tool_name: str, result: dict) -> Any:
    """Raise `pydantic.ValidationError` if `result` does not match the schema."""
    return output_adapter(tool_name).validate_python(result)
