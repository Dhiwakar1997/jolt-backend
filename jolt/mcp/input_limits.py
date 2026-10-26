"""Size limits on MCP tool inputs (BE-10 security review, docs/SECURITY.md).

The MCP body cap (`MCP_REQUEST_MAX_BYTES`, 4 MiB) bounds one message, but not
what a small message can ask for: a few hundred kilobytes of tiny concepts is
thousands of Cosmos writes, and a single string over Cosmos's 2 MB item limit
fails half way through a call. Every tool call is checked here before the
tool runs (jolt/mcp/server.py); an input over a limit fails the call with
"input too large: ..." and writes nothing.

* Lists, by argument or field name (`LIST_LIMITS`), else `DEFAULT_LIST_ITEMS`.
  `log_session` and `store_extraction` accept at most 200 concepts per call:
  a bigger read goes in several calls (reconciliation is already resumable).
* Strings, by name (`TEXT_LIMITS`: the extraction `markdown` up to 500,000
  characters), else `DEFAULT_TEXT_CHARS`.
* `limit` arguments: 1 to `MAX_LIMIT`.

The limits apply at any depth (a concept's `text`, a question's `options`).
"""

from __future__ import annotations

from typing import Any

DEFAULT_TEXT_CHARS = 20_000
DEFAULT_LIST_ITEMS = 100
MAX_DEPTH = 6
MAX_LIMIT = 200

TEXT_LIMITS = {
    # One read of a whole note (30 pages of dense handwriting is ~100k chars).
    "markdown": 500_000,
    "outline": 50_000,
    # Ids, keys and labels.
    "source_id": 200,
    "track_id": 200,
    "concept_id": 200,
    "review_id": 200,
    "concept_key": 200,
    "syllabus_ref": 200,
    "parent": 200,
    "model_id": 200,
    "filename": 255,
    "content_type": 100,
    "sha256": 64,
    "label": 500,
    "title": 500,
}

LIST_LIMITS = {
    "concepts": 200,
    "proposed_concepts": 200,
    "syllabus": 500,
    "source_ids": 50,
    "questions": 20,
    "gradings": 100,
    "errors": 50,
    "options": 10,
}


class InputTooLarge(ValueError):
    """A tool input is over a size limit (an MCP tool error)."""


def _check(name: str, value: Any, depth: int) -> None:
    if depth > MAX_DEPTH:
        raise InputTooLarge(f"input too large: {name} is nested too deeply")
    if isinstance(value, str):
        cap = TEXT_LIMITS.get(name, DEFAULT_TEXT_CHARS)
        if len(value) > cap:
            raise InputTooLarge(
                f"input too large: {name} has {len(value)} characters; at most {cap}"
            )
    elif isinstance(value, (list, tuple)):
        cap = LIST_LIMITS.get(name, DEFAULT_LIST_ITEMS)
        if len(value) > cap:
            raise InputTooLarge(
                f"input too large: {name} has {len(value)} items; at most {cap} per call"
            )
        for item in value:
            _check(name, item, depth + 1)
    elif isinstance(value, dict):
        if len(value) > DEFAULT_LIST_ITEMS:
            raise InputTooLarge(f"input too large: {name} has {len(value)} fields")
        for key, item in value.items():
            _check(str(key), item, depth + 1)


def check_tool_inputs(arguments: dict[str, Any]) -> None:
    """Raise `InputTooLarge` if any argument of a tool call is over its limit."""
    for name, value in arguments.items():
        if name == "limit" and isinstance(value, int) and not isinstance(value, bool):
            if not 1 <= value <= MAX_LIMIT:
                raise InputTooLarge(f"limit must be between 1 and {MAX_LIMIT}")
            continue
        _check(name, value, 0)
