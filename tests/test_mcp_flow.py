"""MCP tools end to end over the in-memory fakes, checked against their schemas.

Walks the nightly loop the way an agent and the app share it: the app uploads a
source, the agent claims and extracts it, logs a session with concepts, writes
questions; the app reviews (stage 1 then stage 2, inline FSRS); the agent grades
the written answer. Every tool result is validated against
`jolt.mcp.schemas.TOOL_OUTPUTS`, the same models exported to
`contract/mcp-tools.json`.
"""

from __future__ import annotations

import hashlib

import httpx
import pytest

from jolt.main import create_app
from jolt.mcp import server as mcp_server
from jolt.mcp.context import MCPAuthError, set_current_token
from jolt.mcp.schemas import TOOL_OUTPUTS, validate_output

TOOLS = dict(mcp_server._TOOLS)


async def call(name: str, **kwargs) -> dict:
    result = await TOOLS[name](**kwargs)
    validate_output(name, result)
    return result


@pytest.fixture
async def api(settings, runtime, mcp_user):
    """Async REST client for the same user/runtime the MCP tools see."""
    app = create_app(settings=settings, runtime=runtime)
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {mcp_user.app_token}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        yield c


def test_every_tool_has_an_output_schema():
    assert set(TOOLS) == set(TOOL_OUTPUTS)


async def test_tools_reject_a_missing_token(mcp_user):
    set_current_token(None)
    with pytest.raises(MCPAuthError):
        await TOOLS["jolt_get_tracks"]()


async def test_agenda_tools(api):
    track = (await api.post("/v1/tracks", json={"name": "Distributed Systems"})).json()
    tid = track["id"]

    agenda = await call("jolt_get_agenda", track_id=tid)
    assert agenda["status"] == "none"

    agenda = await call(
        "jolt_set_agenda",
        track_id=tid,
        syllabus=[
            {"concept_key": "hashing", "label": "Hashing"},
            {"concept_key": "consistent-hashing", "label": "Consistent", "parent": "hashing"},
        ],
        mark_refined=True,
    )
    assert agenda["status"] == "refined"
    assert [i["concept_key"] for i in agenda["syllabus"]] == ["hashing", "consistent-hashing"]

    tracks = await call("jolt_get_tracks")
    assert tracks["tracks"][0]["syllabus"] == ["Hashing", "Consistent"]

    await api.post(f"/v1/tracks/{tid}/agenda/lock")
    rejected = await call("jolt_set_agenda", track_id=tid, syllabus=[])
    assert rejected == {"error": "agenda is locked; refinement is frozen"}
    assert (await call("jolt_get_agenda", track_id="missing")) == {"error": "track not found"}
    bad = await call("jolt_set_agenda", track_id=tid, syllabus=[], source="robot")
    assert "invalid source" in bad["error"]


async def test_nightly_loop_across_mcp_and_rest(api, runtime):
    track = (await api.post("/v1/tracks", json={"name": "DS"})).json()
    tid = track["id"]

    # App: upload one page.
    ticket = (
        await api.post(
            "/v1/uploads/request",
            json={
                "filename": "p1.png",
                "content_type": "image/png",
                "sha256": hashlib.sha256(b"png").hexdigest(),
            },
        )
    ).json()
    blob_path = runtime.cosmos.container("sources").all_items()[0]["blob_path"]
    runtime.blob.put(blob_path, b"png", "image/png")
    assert (await api.post(f"/v1/uploads/{ticket['source_id']}/confirm", json={})).is_success

    # Agent: plan, claim, read, extract.
    plan = await call("jolt_sync")
    assert (plan["unprocessed_sources"], plan["pending_gradings"], plan["done"]) == (1, 0, False)
    assert (plan["rereads"], plan["reread_sources"]) == (0, [])
    claimed = await call("jolt_get_unprocessed_sources")
    assert [s["source_id"] for s in claimed["sources"]] == [ticket["source_id"]]
    assert claimed["sources"][0]["lease_expires_at"] is not None
    # A second claim while the lease is live gets nothing.
    assert (await call("jolt_get_unprocessed_sources"))["sources"] == []

    content = await call("jolt_get_source_content", source_id=ticket["source_id"])
    assert content["read_url"].startswith("memory://blob/")
    ext = await call(
        "jolt_store_extraction",
        source_id=ticket["source_id"],
        markdown="# Consistent hashing\nKeys map to a ring.",
        confidence=0.9,
        model_id="agent-model",
    )
    assert ext["status"] == "active"
    listed = await call("jolt_list_sources")
    assert listed["sources"][0]["processing_status"] == "processed"
    assert listed["sources"][0]["active_extraction_id"] == ext["extraction_id"]

    # Agent: log the session (auto-adds to the agenda) and write a question.
    logged = await call(
        "jolt_log_session",
        source_ids=[ticket["source_id"]],
        concepts=[
            {
                "text": "Consistent hashing maps keys and nodes onto a ring.",
                "title": "Consistent hashing",
                "track_id": tid,
                "concept_key": "consistent-hashing",
                "source_id": ticket["source_id"],
            }
        ],
    )
    (concept_id,) = logged["concept_ids"]
    agenda = await call("jolt_get_agenda", track_id=tid)
    assert agenda["source"] == "emergent"
    assert [i["label"] for i in agenda["syllabus"]] == ["Consistent hashing"]

    recent = await call("jolt_get_recent_concepts", track_id=tid)
    assert recent["concepts"][0]["syllabus_ref"] == "consistent-hashing"
    diff = await call(
        "jolt_diff_extractions",
        track_id=tid,
        proposed_concepts=[{"text": "Consistent hashing maps keys and nodes onto a ring."}],
    )
    assert diff["deltas"][0]["change"] == "unchanged"

    q = await call(
        "jolt_create_questions",
        concept_id=concept_id,
        questions=[
            {
                "stem": "Why does consistent hashing move few keys?",
                "options": ["Only neighbours move", "All keys rehash", "It sorts keys", "Luck"],
                "correct_index": 0,
                "expected_answer": "Only keys between the new node and its predecessor move.",
            }
        ],
    )
    (question_id,) = q["question_ids"]

    # App: due -> stage 1 -> stage 2 (inline FSRS moves the due date).
    due = (await api.get("/v1/reviews/due")).json()
    assert due["count"] == 1 and due["items"][0]["question_id"] == question_id
    s1 = (
        await api.post(
            f"/v1/reviews/{question_id}/stage1",
            json={"concept_id": concept_id, "free_text": "Only nearby keys move"},
        )
    ).json()
    assert len(s1["options"]) == 4
    s2 = (
        await api.post(
            f"/v1/reviews/{s1['review_id']}/stage2", json={"selected_index": 0, "latency_ms": 4000}
        )
    ).json()
    assert s2["correct"] is True and s2["due_at"] is not None
    again = await api.post(f"/v1/reviews/{s1['review_id']}/stage2", json={"selected_index": 0})
    assert again.status_code == 400  # stage 2 is single-shot

    detail = (await api.get(f"/v1/progress/concepts/{concept_id}")).json()
    assert detail["reps"] == 1 and len(detail["timeline"]) == 1
    # Predicted recall is computed (A52): about 1 right after the answer.
    assert 0.99 < detail["retrievability"] <= 1.0 and detail["stability"] > 0

    # Agent: grade the written answer; re-submitting is a no-op.
    pending = await call("jolt_get_pending_gradings")
    assert [g["review_id"] for g in pending["gradings"]] == [s1["review_id"]]
    grading = [{"review_id": s1["review_id"], "suggested_fsrs_grade": 3, "rationale": "ok"}]
    no_rewrites = {"rewritten": [], "rewrites_skipped": []}
    first = await call("jolt_submit_gradings", gradings=grading)
    assert first == {"applied": 1, "submitted": 1, **no_rewrites}
    again = await call("jolt_submit_gradings", gradings=grading)
    assert again == {"applied": 0, "submitted": 1, **no_rewrites}
    assert (await call("jolt_get_pending_gradings"))["gradings"] == []

    cov = await call("jolt_get_coverage", track_id=tid)
    assert cov["total_concepts"] == 1 and cov["studied_concepts"] == 1

    fixed = await call(
        "jolt_correct_concept", track_id=tid, concept_id=concept_id, text="Corrected text."
    )
    assert fixed == {"concept_id": concept_id, "updated": True}
    missing = await call("jolt_correct_concept", track_id=tid, concept_id="nope", text="x")
    assert missing == {"error": "concept not found"}

    url = await call(
        "jolt_request_upload_url",
        filename="p2.pdf",
        content_type="application/pdf",
        sha256="a" * 64,
    )
    assert url["upload_url"].startswith("memory://blob/")
    bad = await call(
        "jolt_request_upload_url", filename="p.gif", content_type="image/gif", sha256="a" * 64
    )
    assert "unsupported content type" in bad["error"]
