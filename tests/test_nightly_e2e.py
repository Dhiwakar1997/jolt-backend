"""End to end (BE-04 §6): two note photos uploaded on day D, one scheduled-task
run that follows the `jolt_sync` plan, and on D+1 the app sees the concepts,
the questions and the sync run.

The "model" is scripted: it does exactly what the plan's steps say, with
canned reading and writing in place of a real model's. The server clock cannot
be moved, so day D is yesterday (UTC) and the run happens now, on D+1.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, time, timedelta, timezone

import httpx
import pytest

from jolt.main import create_app
from jolt.mcp import server as mcp_server
from jolt.mcp.context import set_current_token
from jolt.mcp.schemas import validate_output

TOOLS = dict(mcp_server._TOOLS)
UTC = timezone.utc

# What the scripted model "reads" on each page.
PAGES = {
    "photo-1.jpg": [
        ("Consistent hashing", "consistent-hashing", "Keys and nodes share one hash ring."),
        ("Virtual nodes", "virtual-nodes", "Each node owns many ring points to even out load."),
    ],
    "photo-2.jpg": [
        ("Quorum reads", "quorum-reads", "R + W > N makes reads see the latest write."),
    ],
}


async def call(name: str, **kwargs) -> dict:
    result = await TOOLS[name](**kwargs)
    validate_output(name, result)
    assert "error" not in result, result
    return result


async def scripted_model(choose_track) -> list[dict]:
    """Follow the plan until it says done; return every plan seen."""
    plans = [await call("jolt_sync")]
    while not plans[-1]["done"]:
        plan = plans[-1]
        steps = {s["tool"]: s for s in plan["steps"]}
        assert plan["steps"][-1]["tool"] == "jolt_sync"

        work = list(plan["claimed_sources"])
        if "jolt_get_unprocessed_sources" in steps:
            limit = steps["jolt_get_unprocessed_sources"]["limit"]
            claimed = await call("jolt_get_unprocessed_sources", limit=limit)
            assert len(claimed["sources"]) <= limit
            work += claimed["sources"]
        for source in work:
            content = await call("jolt_get_source_content", source_id=source["source_id"])
            found = PAGES[content["filename"]]
            await call(
                "jolt_store_extraction",
                source_id=source["source_id"],
                markdown="\n".join(f"## {t}\n{text}" for t, _, text in found),
                confidence=0.92,
                model_id="scripted-model",
            )
            track_id = source.get("track_id") or choose_track(plan)
            logged = await call(
                "jolt_log_session",
                source_ids=[source["source_id"]],
                concepts=[
                    {
                        "text": text,
                        "title": title,
                        "concept_key": key,
                        "track_id": track_id,
                        "source_id": source["source_id"],
                    }
                    for title, key, text in found
                ][: steps["jolt_log_session"]["limit"]],
            )
            for (title, _, text), concept_id in zip(found, logged["concept_ids"]):
                options = [text, "A distractor", "Another distractor", "A third distractor"]
                assert len(options) == plan["limits"]["options_per_question"]
                await call(
                    "jolt_create_questions",
                    concept_id=concept_id,
                    questions=[
                        {
                            "stem": f"Explain {title.lower()} in your own words.",
                            "options": options,
                            "correct_index": 0,
                            "expected_answer": text,
                        }
                    ],
                )

        if "jolt_get_pending_gradings" in steps:
            limit = steps["jolt_get_pending_gradings"]["limit"]
            pending = await call("jolt_get_pending_gradings", limit=limit)
            await call(
                "jolt_submit_gradings",
                gradings=[
                    {
                        "review_id": g["review_id"],
                        "suggested_fsrs_grade": 3 if g["free_text"].strip() else 1,
                        "rationale": "Scripted grade.",
                        "rubric_version": "test",
                    }
                    for g in pending["gradings"]
                ],
            )
        plans.append(await call("jolt_sync"))
        assert len(plans) < 10, "the plan never finished"
    return plans


@pytest.fixture
async def app_client(settings, runtime, mcp_user):
    app = create_app(settings=settings, runtime=runtime)
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {mcp_user.app_token}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        yield c


async def test_upload_on_day_d_sync_overnight_see_it_on_d_plus_1(app_client, runtime, mcp_user):
    api = app_client
    day_d = datetime.now(UTC).date() - timedelta(days=1)

    # The learner connects Claude Desktop: an MCP token labelled for it.
    token = (await api.post("/v1/mcp-tokens", json={"label": "Claude Desktop"})).json()
    set_current_token(token["token"])
    track = (await api.post("/v1/tracks", json={"name": "Distributed systems"})).json()

    # An answer from earlier is waiting for its semantic grade.
    earlier = await call(
        "jolt_log_session",
        source_ids=[],
        concepts=[{"text": "CAP: pick two of three.", "track_id": track["id"]}],
    )
    (old_concept,) = earlier["concept_ids"]
    (old_q,) = (
        await call(
            "jolt_create_questions",
            concept_id=old_concept,
            questions=[
                {
                    "stem": "What does CAP say?",
                    "options": ["Pick two", "Pick one", "Pick all", "None"],
                    "correct_index": 0,
                    "expected_answer": "Under a partition, choose consistency or availability.",
                }
            ],
        )
    )["question_ids"]

    # Day D: two note photos uploaded in the evening, and that earlier answer.
    sources = []
    for i, name in enumerate(PAGES):
        data = f"jpeg bytes {i}".encode()
        ticket = (
            await api.post(
                "/v1/uploads/request",
                json={
                    "filename": name,
                    "content_type": "image/jpeg",
                    "sha256": hashlib.sha256(data).hexdigest(),
                },
            )
        ).json()
        source = await runtime.repos.sources.get_source(ticket["source_id"], mcp_user.user_id)
        runtime.blob.put(source.blob_path, data, "image/jpeg")
        assert (await api.post(f"/v1/uploads/{source.id}/confirm", json={})).is_success
        source = await runtime.repos.sources.get_source(source.id, mcp_user.user_id)
        source.created_at = datetime.combine(day_d, time(20 + i, 30), tzinfo=UTC)
        await runtime.repos.sources.replace(source)
        sources.append(source)
    s1 = (
        await api.post(
            f"/v1/reviews/{old_q}/stage1",
            json={"concept_id": old_concept, "free_text": "Consistency or availability"},
        )
    ).json()
    await api.post(f"/v1/reviews/{s1['review_id']}/stage2", json={"selected_index": 0})
    status = (await api.get("/v1/sync/status")).json()
    assert status["health"] == "never" and status["last_run"] is None

    # 00:15 on D+1: the scheduled task runs once.
    plans = await scripted_model(choose_track=lambda plan: plan["tracks"][0]["track_id"])
    first = plans[0]
    assert first["unprocessed_sources"] == 2 and first["pending_gradings"] == 1
    assert first["upload_dates"] == [day_d.isoformat()]
    assert plans[-1]["done"] and len({p["run_id"] for p in plans}) == 1

    # D+1, in the app: the sync run.
    status = (await api.get("/v1/sync/status")).json()
    assert status["health"] == "ok"
    run = status["last_run"]
    assert run["status"] == "finished" and run["client_label"] == "Claude Desktop"
    assert run["sources_processed"] == 2
    assert run["questions_written"] == 3
    assert run["gradings_submitted"] == 1
    assert run["errors"] == []
    titles = [c["concept_title"] for c in run["changes"]]
    assert titles == ["Consistent hashing", "Virtual nodes", "Quorum reads"]
    assert all(c["kind"] == "concept_added" and c["added_to_syllabus"] for c in run["changes"])
    assert {datetime.fromisoformat(c["source_uploaded_at"]).date() for c in run["changes"]} == {
        day_d
    }
    assert (await api.get("/v1/sync/runs")).json() == [run]

    # The sources are read, the concepts and questions are due.
    listed = {s["id"]: s for s in (await api.get("/v1/sources")).json()}
    assert all(listed[s.id]["processing_status"] == "processed" for s in sources)
    due = (await api.get("/v1/reviews/due")).json()
    new_ids = {c["concept_id"] for c in run["changes"]}
    assert new_ids <= {i["concept_id"] for i in due["items"]}
    coverage = (await api.get(f"/v1/tracks/{track['id']}/coverage")).json()
    assert coverage["total_concepts"] == 4

    # The track's agenda grew from the notes; one session for day D (D6).
    tracks = (await api.get("/v1/tracks")).json()
    keys = [i["concept_key"] for i in tracks[0]["agenda"]["syllabus"]]
    assert keys == ["consistent-hashing", "virtual-nodes", "quorum-reads"]
    sessions = await runtime.repos.sessions.list_for_user(mcp_user.user_id)
    day_sessions = [s for s in sessions if s.upload_date == day_d.isoformat()]
    # The model logged each photo on its own; both land in day D's one session.
    assert len(day_sessions) == 1
    assert day_sessions[0].source_ids == [s.id for s in sources]
    assert len(day_sessions[0].concept_ids) == 3
    assert day_sessions[0].sync_run_id == run["id"]

    # The earlier answer got its grade; onboarding sees the first sync.
    assert (await call("jolt_get_pending_gradings"))["gradings"] == []
    me = (await api.get("/v1/me")).json()
    assert me["onboarding"]["first_sync_at"] is not None
