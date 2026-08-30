"""Tool descriptions — the agent-facing contract (LLD §1: the MCP surface is the
product). Kept as data so they are reviewed on change alongside the rubric.
"""

from __future__ import annotations

TOOL_DESCRIPTIONS: dict[str, str] = {
    # read
    "jolt_get_tracks": "List the caller's tracks plus world-readable curated tracks.",
    "jolt_get_recent_concepts": "Recent concepts for a track, newest first.",
    "jolt_get_coverage": "Coverage of a track's syllabus by the caller's studied concepts.",
    # write
    "jolt_request_upload_url": (
        "Get a short-TTL SAS URL to upload a source file straight to Blob. Jolt "
        "compute never touches the bytes."
    ),
    "jolt_log_session": (
        "Record a study session: create concepts (each with its track_id) and a "
        "fresh, immediately-due FSRS card per concept."
    ),
    "jolt_store_extraction": (
        "Store the markdown extraction for a source and mark it processed. Set "
        "supersede=true to replace the source's active extraction."
    ),
    "jolt_create_questions": "Attach review questions (stem, options, correct_index, expected_answer) to a concept.",
    "jolt_correct_concept": "Replace a concept's text with an agent-supplied correction.",
    # sync
    "jolt_sync": "Return the sync plan: count of unprocessed sources and pending gradings.",
    "jolt_get_unprocessed_sources": (
        "Claim up to `limit` unprocessed sources under a lease. Interrupted claims "
        "become reclaimable automatically once their lease expires."
    ),
    "jolt_get_pending_gradings": (
        "Claim up to `limit` reviews awaiting a semantic grade, with the learner's "
        "free-text and the expected answer."
    ),
    "jolt_submit_gradings": (
        "Submit semantic FSRS grades (see the grading rubric). Idempotent per "
        "review_id; each affected card is re-folded inline."
    ),
    # extract
    "jolt_list_sources": "List all of the caller's sources and their processing status.",
    "jolt_get_source_content": "Get a short-TTL signed GET URL to read a source's original bytes.",
    "jolt_diff_extractions": (
        "Preview the per-concept classification (unchanged/refined/changed/removed/"
        "new) a supersede would produce, without committing it."
    ),
}
