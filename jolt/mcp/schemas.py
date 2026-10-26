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


class AgendaOut(_Out):
    status: Literal["none", "draft", "refined", "locked"]
    source: Literal["agent", "user", "emergent"]
    syllabus: list[SyllabusItemOut]
    last_refined_at: Optional[str]  # ISO-8601 UTC
    refined_from_source_id: Optional[str]


class TrackOut(_Out):
    id: str
    name: str
    origin: Literal["user", "jolt"]
    syllabus: list[str]
    agenda: AgendaOut


class GetTracksOut(_Out):
    tracks: list[TrackOut]


class RecentConceptOut(_Out):
    id: str
    text: str
    syllabus_ref: Optional[str]


class GetRecentConceptsOut(_Out):
    concepts: list[RecentConceptOut]


class GetCoverageOut(_Out):
    track_id: str
    total_concepts: int
    studied_concepts: int
    decaying_concepts: int
    coverage_ratio: float
    uncovered_syllabus: list[str]


# -- writes ----------------------------------------------------------------
class RequestUploadUrlOut(_Out):
    source_id: str
    upload_url: str


class LogSessionOut(_Out):
    session_id: str
    concept_ids: list[str]


class StoreExtractionOut(_Out):
    extraction_id: str
    status: Literal["active", "superseded"]


class CreateQuestionsOut(_Out):
    question_ids: list[str]


class CorrectConceptOut(_Out):
    concept_id: str
    updated: bool


# -- sync ------------------------------------------------------------------
class RereadOut(_Out):
    source_id: str
    title: str
    preferred_model: Optional[str]
    requested_at: str  # ISO-8601 UTC


class SyncOut(_Out):
    unprocessed_sources: int
    pending_gradings: int
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
    "jolt_request_upload_url": Union[RequestUploadUrlOut, ToolError],
    "jolt_log_session": LogSessionOut,
    "jolt_set_agenda": Union[AgendaOut, ToolError],
    "jolt_store_extraction": StoreExtractionOut,
    "jolt_create_questions": CreateQuestionsOut,
    "jolt_correct_concept": Union[CorrectConceptOut, ToolError],
    "jolt_sync": SyncOut,
    "jolt_get_unprocessed_sources": GetUnprocessedSourcesOut,
    "jolt_get_pending_gradings": GetPendingGradingsOut,
    "jolt_submit_gradings": SubmitGradingsOut,
    "jolt_list_sources": ListSourcesOut,
    "jolt_get_source_content": GetSourceContentOut,
    "jolt_diff_extractions": DiffExtractionsOut,
}


def output_adapter(tool_name: str) -> TypeAdapter:
    return TypeAdapter(TOOL_OUTPUTS[tool_name])


def output_schema(tool_name: str) -> dict:
    """JSON Schema for a tool's result."""
    return output_adapter(tool_name).json_schema()


def validate_output(tool_name: str, result: dict) -> Any:
    """Raise `pydantic.ValidationError` if `result` does not match the schema."""
    return output_adapter(tool_name).validate_python(result)
