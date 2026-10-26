"""BE-06 review loop end to end over the in-memory fakes.

Sessions (ordered, interleaved queue → stage 1 → stage 2 → summary with what
moved), idempotent stages, the server-side stage lock, question versions from
graded rewrites (decision D8), and mastered retirement with the setting on and
off. The agent side runs through the real MCP tool functions, validated against
their output schemas; the app side through the REST routes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from jolt.domain.models import ConceptState
from jolt.main import create_app
from jolt.mcp import server as mcp_server
from jolt.mcp.schemas import validate_output
from jolt.services.reviews import interleave

TOOLS = dict(mcp_server._TOOLS)
LONG = (
    "Consistent hashing puts keys and nodes on one ring; each key belongs to the next "
    "node clockwise, so adding a node only moves the keys between it and its predecessor."
)
assert len(LONG) >= 120  # a thorough recall: Good (or Easy) when correct


async def call(name: str, **kwargs) -> dict:
    result = await TOOLS[name](**kwargs)
    validate_output(name, result)
    return result


@pytest.fixture
async def api(settings, runtime, mcp_user):
    app = create_app(settings=settings, runtime=runtime)
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {mcp_user.app_token}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        yield c


async def ok(resp: httpx.Response, status: int = 200) -> dict:
    assert resp.status_code == status, resp.text
    return resp.json()


def question(stem: str) -> dict:
    return {
        "stem": stem,
        "options": ["right", "wrong 1", "wrong 2", "wrong 3"],
        "correct_index": 0,
        "expected_answer": f"The full answer to: {stem}",
    }


async def new_track(api, name: str) -> str:
    return (await ok(await api.post("/v1/tracks", json={"name": name})))["id"]


async def new_concept(track_id: str, title: str, questions: int = 1) -> tuple[str, list[str]]:
    logged = await call(
        "jolt_log_session",
        source_ids=[],
        concepts=[{"text": f"{title} explained.", "title": title, "track_id": track_id}],
    )
    (concept_id,) = logged["concept_ids"]
    created = await call(
        "jolt_create_questions",
        concept_id=concept_id,
        questions=[question(f"{title} Q{i + 1}?") for i in range(questions)],
    )
    return concept_id, created["question_ids"]


async def answer(api, item: dict, *, correct=True, text="short", latency=10_000, session=None):
    s1 = await ok(
        await api.post(
            f"/v1/reviews/{item['question_id']}/stage1",
            json={"concept_id": item["concept_id"], "free_text": text, "session_id": session},
        )
    )
    s2 = await ok(
        await api.post(
            f"/v1/reviews/{s1['review_id']}/stage2",
            json={"selected_index": 0 if correct else 1, "latency_ms": latency},
        )
    )
    return s1, s2


async def make_due(runtime, user_id: str, concept_id: str) -> None:
    state = await runtime.repos.concept_states.for_concept(user_id, concept_id)
    state.due_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    assert await runtime.repos.concept_states.replace(state, etag=state.etag) is not None


async def other_user_headers(api) -> dict:
    signed = await ok(await api.post("/v1/auth/dev", json={"email": "eve@example.com"}))
    return {"Authorization": f"Bearer {signed['app_token']}"}


# --------------------------------------------------------------------------- #
def _state(track: str, minutes_overdue: int) -> ConceptState:
    due = datetime(2026, 1, 1, tzinfo=timezone.utc) - timedelta(minutes=minutes_overdue)
    cid = f"{track}{minutes_overdue}"
    return ConceptState(user_id="u", concept_id=cid, track_id=track, due_at=due)


def test_interleave_is_most_overdue_first_alternating_tracks():
    states = [_state("a", 50), _state("a", 40), _state("a", 30), _state("b", 45), _state("c", 5)]
    order = [s.concept_id for s in interleave(states)]
    # Round 1: heads a50, b45, c5 (most overdue head first); then a40; then a30.
    assert order == ["a50", "b45", "c5", "a40", "a30"]
    assert interleave([]) == []


async def test_full_session_start_answer_end_with_what_moved(api, runtime, mcp_user):
    ta, tb = await new_track(api, "Systems"), await new_track(api, "Biology")
    a1, _ = await new_concept(ta, "Consistent hashing")
    a2, _ = await new_concept(ta, "Quorums")
    b1, _ = await new_concept(tb, "Mitosis")

    session = await ok(await api.post("/v1/review-sessions", json={}))
    items = session["items"]
    assert [i["concept_id"] for i in items] == [a1, b1, a2]  # interleaved tracks
    first = items[0]
    assert first["concept_title"] == "Consistent hashing"
    assert first["track_name"] == "Systems" and first["track_id"] == ta
    assert first["question_type"] == "free_text" and first["pattern_match"] is None
    assert (first["version"], first["review_number"], first["rewritten"]) == (1, 1, False)
    assert first["interval_days"] is None
    # Server-side stage lock: nothing that could reveal the answer before stage 1.
    for item in items:
        assert not {"options", "correct_index", "expected_answer"} & set(item)
    assert session["done_today"] == 0 and session["track_id"] is None

    results = []
    for n, item in enumerate(items):
        s1, s2 = await answer(api, item, correct=n != 1, session=session["id"])
        assert len(s1["options"]) == 4 and s1["locked_at"]
        assert s2["grading"] == "provisional" and s2["feedback"] is None
        assert s2["provisional_grade"] == (2 if n != 1 else 1)  # short recall: Hard / Again
        assert s2["interval_days"] >= 1 and s2["due_at"] and s2["retired"] is False
        assert s2["match"] is None and s2["question_title"] is None
        results.append(s2)

    summary = await ok(await api.post(f"/v1/review-sessions/{session['id']}/end"))
    assert summary["session_id"] == session["id"]
    assert (summary["reviewed"], summary["correct"]) == (3, 2)
    assert (summary["provisional"], summary["graded"]) == (3, 0)
    assert summary["retired"] == []
    moved = {m["concept_id"]: m for m in summary["moved"]}
    assert set(moved) == {a1, a2, b1}
    for m in moved.values():
        assert m["old_due_at"] and m["new_due_at"] and m["new_due_at"] > m["old_due_at"]
    assert moved[a1]["title"] == "Consistent hashing"
    assert len(summary["upcoming"]) == 7
    assert all(set(d) == {"date", "questions"} for d in summary["upcoming"])

    # Ending again returns the same summary; nothing is due right after.
    again = await ok(await api.post(f"/v1/review-sessions/{session['id']}/end"))
    assert again["ended_at"] == summary["ended_at"] and again["reviewed"] == 3
    nxt = await ok(await api.post("/v1/review-sessions", json={"track_id": ta}))
    assert nxt["items"] == [] and nxt["done_today"] == 3

    # Grading the answers moves them from provisional to graded, with feedback.
    pending = await call("jolt_get_pending_gradings")
    assert len(pending["gradings"]) == 3 and pending["allow_question_rewrite"] is False
    g0 = pending["gradings"][0]
    assert g0["stem"] and len(g0["options"]) == 4 and g0["question_version"] == 1
    assert g0["selected_index"] is not None and g0["is_correct"] is not None
    await call(
        "jolt_submit_gradings",
        gradings=[
            {"review_id": g["review_id"], "suggested_fsrs_grade": 3, "rationale": "Well put."}
            for g in pending["gradings"]
        ],
    )
    graded = await ok(await api.post(f"/v1/review-sessions/{session['id']}/end"))
    assert (graded["provisional"], graded["graded"]) == (0, 3)
    review = await ok(await api.get(f"/v1/reviews/{pending['gradings'][0]['review_id']}"))
    assert review["grading"] == "graded" and review["feedback"] == "Well put."
    assert review["session_id"] == session["id"] and review["final_grade"] == 3
    timeline = (await ok(await api.get(f"/v1/progress/concepts/{a1}")))["timeline"]
    assert timeline[0]["grading"] == "graded" and timeline[0]["feedback"] == "Well put."


async def test_session_filters_and_errors(api, runtime, mcp_user):
    ta, tb = await new_track(api, "A"), await new_track(api, "B")
    a1, _ = await new_concept(ta, "Alpha")
    b1, _ = await new_concept(tb, "Beta")
    await new_concept(tb, "No questions", questions=0)  # due, but cannot be reviewed

    only_b = await ok(await api.post("/v1/review-sessions", json={"track_id": tb}))
    assert [i["concept_id"] for i in only_b["items"]] == [b1]
    assert only_b["track_id"] == tb
    limited = await ok(await api.post("/v1/review-sessions", json={"limit": 1}))
    assert [i["concept_id"] for i in limited["items"]] == [a1]
    practise = await ok(await api.post("/v1/review-sessions", json={"concept_id": b1}))
    assert [i["concept_id"] for i in practise["items"]] == [b1]
    dsa = await ok(await api.post("/v1/review-sessions", json={"kind": "dsa_patterns"}))
    assert dsa["items"] == []

    assert (await api.post("/v1/review-sessions", json={"track_id": "nope"})).status_code == 404
    assert (await api.post("/v1/review-sessions", json={"concept_id": "nope"})).status_code == 404
    assert (await api.post("/v1/review-sessions", json={"limit": 0})).status_code == 422
    assert (await api.post("/v1/review-sessions/nope/end")).status_code == 404
    bad_session = await api.post(
        f"/v1/reviews/{only_b['items'][0]['question_id']}/stage1",
        json={"concept_id": b1, "free_text": "x", "session_id": "nope"},
    )
    assert bad_session.status_code == 404

    # Another user sees none of it.
    eve = await other_user_headers(api)
    item = only_b["items"][0]
    stolen = await api.post(
        f"/v1/reviews/{item['question_id']}/stage1",
        json={"concept_id": b1, "free_text": "x"},
        headers=eve,
    )
    assert stolen.status_code == 404
    assert (
        await api.post(f"/v1/review-sessions/{only_b['id']}/end", headers=eve)
    ).status_code == 404
    assert (
        await api.post("/v1/review-sessions", json={"concept_id": b1}, headers=eve)
    ).status_code == 404
    assert (await api.post(f"/v1/concepts/{b1}/unretire", headers=eve)).status_code == 404
    assert (await api.get(f"/v1/concepts/{b1}/questions", headers=eve)).status_code == 404


async def test_both_stages_are_idempotent_on_client_request_id(api, runtime, mcp_user):
    t = await new_track(api, "T")
    c1, (q1,) = await new_concept(t, "One")
    c2, (q2,) = await new_concept(t, "Two")

    body = {"concept_id": c1, "free_text": "first words", "client_request_id": "s1-abc"}
    first = await ok(await api.post(f"/v1/reviews/{q1}/stage1", json=body))
    retry = await ok(
        await api.post(f"/v1/reviews/{q1}/stage1", json={**body, "free_text": "changed"})
    )
    assert retry == first  # same review, same lock time, same options
    stored = await runtime.repos.reviews.get_review(first["review_id"], mcp_user.user_id)
    assert stored.free_text == "first words"  # the lock holds
    assert len(runtime.cosmos.container("reviews").all_items()) == 1
    reused = await api.post(
        f"/v1/reviews/{q2}/stage1",
        json={"concept_id": c2, "free_text": "x", "client_request_id": "s1-abc"},
    )
    assert reused.status_code == 409

    s2_body = {"selected_index": 0, "latency_ms": 5000, "client_request_id": "s2-abc"}
    s2 = await ok(await api.post(f"/v1/reviews/{first['review_id']}/stage2", json=s2_body))
    s2_retry = await ok(await api.post(f"/v1/reviews/{first['review_id']}/stage2", json=s2_body))
    assert s2_retry == s2
    state = await runtime.repos.concept_states.for_concept(mcp_user.user_id, c1)
    assert state.reps == 1  # the retry did not fold a second answer
    other_key = {**s2_body, "client_request_id": "s2-other"}
    resp = await api.post(f"/v1/reviews/{first['review_id']}/stage2", json=other_key)
    assert resp.status_code == 400 and resp.json()["detail"] == "stage 2 already submitted"
    no_key = await api.post(f"/v1/reviews/{first['review_id']}/stage2", json={"selected_index": 0})
    assert no_key.status_code == 400

    s1b = await ok(
        await api.post(f"/v1/reviews/{q2}/stage1", json={"concept_id": c2, "free_text": "y"})
    )
    out_of_range = await api.post(
        f"/v1/reviews/{s1b['review_id']}/stage2", json={"selected_index": 9}
    )
    assert out_of_range.status_code == 400


async def test_settings_defaults_and_patch(api):
    assert await ok(await api.get("/v1/settings")) == {
        "retire_mastered": True,
        "allow_question_rewrite": False,  # decision D8: opt-in
    }
    patched = await ok(await api.patch("/v1/settings", json={"allow_question_rewrite": True}))
    assert patched == {"retire_mastered": True, "allow_question_rewrite": True}
    patched = await ok(await api.patch("/v1/settings", json={"retire_mastered": False}))
    assert patched == {"retire_mastered": False, "allow_question_rewrite": True}
    assert await ok(await api.get("/v1/settings")) == patched
    assert await ok(await api.patch("/v1/settings", json={})) == patched


async def _practise(api, concept_id: str, **kw) -> tuple[dict, dict]:
    session = await ok(await api.post("/v1/review-sessions", json={"concept_id": concept_id}))
    (item,) = session["items"]
    s1, s2 = await answer(api, item, session=session["id"], **kw)
    return item, s1


def rewrite(stem: str, reason: str = "Probe the part you missed") -> dict:
    return {**question(stem), "rewrite_reason": reason}


async def test_rewrites_make_versions_only_when_allowed(api, runtime, mcp_user):
    t = await new_track(api, "T")
    concept, (v1,) = await new_concept(t, "Hashing")

    # Off (the default): the grade lands, the rewrite is skipped.
    _, s1 = await _practise(api, concept)
    out = await call(
        "jolt_submit_gradings",
        gradings=[
            {
                "review_id": s1["review_id"],
                "suggested_fsrs_grade": 2,
                "rewritten_question": rewrite("Never stored?"),
            }
        ],
    )
    assert out["applied"] == 1 and out["rewritten"] == []
    assert out["rewrites_skipped"][0]["reason"].startswith("question rewriting is off")
    versions = await ok(await api.get(f"/v1/concepts/{concept}/questions"))
    assert [q["version"] for q in versions["question_versions"]] == [1]

    # On: version 2, version 1 kept but retired from the queue.
    await ok(await api.patch("/v1/settings", json={"allow_question_rewrite": True}))
    assert (await call("jolt_get_pending_gradings"))["allow_question_rewrite"] is True
    _, s1 = await _practise(api, concept)
    grading = {
        "review_id": s1["review_id"],
        "suggested_fsrs_grade": 2,
        "rationale": "You named the ring but not which keys move.",
        "rewritten_question": rewrite("Which keys move when a node joins?"),
    }
    out = await call("jolt_submit_gradings", gradings=[grading])
    (made,) = out["rewritten"]
    assert made["version"] == 2 and made["previous_version_id"] == v1
    assert out["rewrites_skipped"] == []
    v2 = made["question_id"]

    # A retry does not fork the chain: the same version is reported again.
    retry = await call("jolt_submit_gradings", gradings=[grading])
    assert retry["applied"] == 0 and retry["rewritten"] == [made]

    versions = await ok(await api.get(f"/v1/concepts/{concept}/questions"))
    chain = versions["question_versions"]
    assert [(q["question_id"], q["version"], q["active"]) for q in chain] == [
        (v1, 1, False),
        (v2, 2, True),
    ]
    assert chain[1]["previous_version_id"] == v1
    assert chain[1]["rewrite_reason"] == "Probe the part you missed"
    assert versions["current_question"]["question_id"] == v2
    review = await ok(await api.get(f"/v1/reviews/{s1['review_id']}"))
    assert review["rewritten_question_id"] == v2

    # The queue serves version 2 next time, marked as rewritten.
    await make_due(runtime, mcp_user.user_id, concept)
    session = await ok(await api.post("/v1/review-sessions", json={}))
    (item,) = session["items"]
    assert (item["question_id"], item["version"], item["rewritten"]) == (v2, 2, True)
    assert item["stem"] == "Which keys move when a node joins?"
    due = await ok(await api.get("/v1/reviews/due"))
    assert [i["question_id"] for i in due["items"]] == [v2]
    # Answering it records the version on the review.
    s1, _ = await answer(api, item, session=session["id"])
    pending = await call("jolt_get_pending_gradings")
    (p,) = [g for g in pending["gradings"] if g["review_id"] == s1["review_id"]]
    assert p["question_version"] == 2 and p["question_id"] == v2

    # A rewrite graded on an old version extends the chain from its head (v3 after v2).
    out = await call(
        "jolt_submit_gradings",
        gradings=[
            {
                "review_id": s1["review_id"],
                "suggested_fsrs_grade": 3,
                "rewritten_question": rewrite("Now from a new angle?", reason=""),
            }
        ],
    )
    (v3,) = out["rewritten"]
    assert v3["version"] == 3 and v3["previous_version_id"] == v2
    chain = (await ok(await api.get(f"/v1/concepts/{concept}/questions")))["question_versions"]
    assert [q["version"] for q in chain] == [1, 2, 3]
    assert [q["active"] for q in chain] == [False, False, True]
    assert chain[2]["rewrite_reason"] is None

    # An invalid rewrite is skipped with a reason; the grade still lands.
    _, s1 = await _practise(api, concept)
    out = await call(
        "jolt_submit_gradings",
        gradings=[
            {
                "review_id": s1["review_id"],
                "suggested_fsrs_grade": 3,
                "rewritten_question": {**rewrite("Bad?"), "correct_index": 7},
            }
        ],
    )
    assert out["applied"] == 1 and out["rewritten"] == []
    assert "correct_index" in out["rewrites_skipped"][0]["reason"]


async def test_concurrent_rewrites_of_one_version_do_not_fork(api, runtime, mcp_user):
    from jolt.domain.grading import QuestionRewrite
    from jolt.services.gradings import GradingService

    t = await new_track(api, "T")
    concept, (v1,) = await new_concept(t, "Race")
    _, s1a = await _practise(api, concept)
    _, s1b = await _practise(api, concept)
    ra = await runtime.repos.reviews.get_review(s1a["review_id"], mcp_user.user_id)
    rb = await runtime.repos.reviews.get_review(s1b["review_id"], mcp_user.user_id)
    service = GradingService(runtime)
    one = await service.create_version(ra, QuestionRewrite(**question("A?")))
    two = await service.create_version(rb, QuestionRewrite(**question("B?")))
    again = await service.create_version(ra, QuestionRewrite(**question("A?")))
    assert (one.version, one.previous_version_id) == (2, v1)
    assert (two.version, two.previous_version_id) == (3, one.id)
    assert again.id == one.id
    active = await runtime.repos.questions.list_for_concept(concept)
    assert [q.id for q in active] == [two.id]


async def test_mastered_concepts_leave_the_queue_only_with_the_setting(
    api, runtime, mcp_user, monkeypatch
):
    # A low threshold so one Good answer (stability ~3.2 days) counts as mastered.
    monkeypatch.setattr(runtime.settings, "mastery_stability_days", 3.0)
    t = await new_track(api, "T")
    concept, _ = await new_concept(t, "Sticky")
    other, _ = await new_concept(t, "Other")

    session = await ok(await api.post("/v1/review-sessions", json={"concept_id": concept}))
    (item,) = session["items"]
    _, s2 = await answer(api, item, text=LONG, latency=12_000, session=session["id"])
    assert s2["provisional_grade"] == 3 and s2["retired"] is True
    summary = await ok(await api.post(f"/v1/review-sessions/{session['id']}/end"))
    assert summary["retired"] == [{"concept_id": concept, "title": "Sticky"}]
    (moved,) = summary["moved"]
    assert moved["concept_id"] == concept and moved["new_due_at"] is None
    state = await runtime.repos.concept_states.for_concept(mcp_user.user_id, concept)
    assert state.mastered_at is not None and state.stability >= 3.0

    # Even once its FSRS date passes, it stays out of the queue and the due list.
    await make_due(runtime, mcp_user.user_id, concept)
    queue = await ok(await api.post("/v1/review-sessions", json={}))
    assert [i["concept_id"] for i in queue["items"]] == [other]
    due = await ok(await api.get("/v1/reviews/due"))
    assert concept not in {i["concept_id"] for i in due["items"]}

    # Setting off: it is back at once. On again: gone again.
    await ok(await api.patch("/v1/settings", json={"retire_mastered": False}))
    queue = await ok(await api.post("/v1/review-sessions", json={}))
    assert {i["concept_id"] for i in queue["items"]} == {concept, other}
    await ok(await api.patch("/v1/settings", json={"retire_mastered": True}))
    queue = await ok(await api.post("/v1/review-sessions", json={}))
    assert [i["concept_id"] for i in queue["items"]] == [other]

    # Unretire: back in the queue, due now; unretiring again is a no-op.
    un = await ok(await api.post(f"/v1/concepts/{concept}/unretire"))
    assert un["concept_id"] == concept and un["retired"] is False and un["due_at"]
    queue = await ok(await api.post("/v1/review-sessions", json={}))
    assert concept in {i["concept_id"] for i in queue["items"]}
    assert (await ok(await api.post(f"/v1/concepts/{concept}/unretire")))["retired"] is False
    assert (await api.post("/v1/concepts/nope/unretire")).status_code == 404

    # A later answer that is still mastered retires it again.
    item = next(i for i in queue["items"] if i["concept_id"] == concept)
    _, s2 = await answer(api, item, text=LONG, latency=12_000, session=queue["id"])
    assert s2["retired"] is True


async def test_mastered_concepts_stay_with_the_setting_off(api, runtime, mcp_user, monkeypatch):
    monkeypatch.setattr(runtime.settings, "mastery_stability_days", 3.0)
    await ok(await api.patch("/v1/settings", json={"retire_mastered": False}))
    t = await new_track(api, "T")
    concept, _ = await new_concept(t, "Sticky")

    session = await ok(await api.post("/v1/review-sessions", json={}))
    _, s2 = await answer(api, session["items"][0], text=LONG, latency=12_000)
    assert s2["retired"] is False
    state = await runtime.repos.concept_states.for_concept(mcp_user.user_id, concept)
    assert state.mastered_at is not None  # mastered, but kept in rotation
    await make_due(runtime, mcp_user.user_id, concept)
    queue = await ok(await api.post("/v1/review-sessions", json={}))
    assert [i["concept_id"] for i in queue["items"]] == [concept]

    # A lapse un-masters it.
    _, s2 = await answer(api, queue["items"][0], correct=False)
    state = await runtime.repos.concept_states.for_concept(mcp_user.user_id, concept)
    assert state.mastered_at is None


async def test_rewrites_are_recorded_on_the_sync_run(api, runtime, mcp_user):
    """BE-04 x BE-06: gradings count on the run; a new version is a change once."""
    t = await new_track(api, "T")
    concept, _ = await new_concept(t, "Hashing")
    await ok(await api.patch("/v1/settings", json={"allow_question_rewrite": True}))
    _, s1 = await _practise(api, concept)

    run_id = (await call("jolt_sync"))["run_id"]
    grading = {
        "review_id": s1["review_id"],
        "suggested_fsrs_grade": 3,
        "rationale": "Right idea.",
        "rewritten_question": rewrite("Which keys move?"),
    }
    out = await call("jolt_submit_gradings", gradings=[grading])
    assert out["applied"] == 1 and len(out["rewritten"]) == 1
    retry = await call("jolt_submit_gradings", gradings=[grading])
    assert retry["applied"] == 0 and retry["rewritten"] == out["rewritten"]

    run = (await ok(await api.get("/v1/sync/status")))["last_run"]
    assert run["id"] == run_id and run["gradings_submitted"] == 1
    rewrites = [c for c in run["changes"] if c["kind"] == "question_rewritten"]
    assert [(c["concept_id"], c["concept_title"], c["track_id"]) for c in rewrites] == [
        (concept, "Hashing", t)
    ]
