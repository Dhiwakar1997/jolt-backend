"""BE-07: Tracks, Track detail and Concept read models, track create / rename /
archive, outline drafts, off-plan concepts, corrections, and the no-N+1 budget,
over the in-memory fakes (A8).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from jolt.domain.agenda import outline_to_syllabus
from jolt.main import create_app
from tests.journeys import (
    LONG,
    answer,
    call,
    count_cosmos_calls,
    make_due,
    new_concept,
    new_track,
    ok,
    question,
    read_source,
    set_card,
    upload,
)


@pytest.fixture
async def api(settings, runtime, mcp_user):
    app = create_app(settings=settings, runtime=runtime)
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {mcp_user.app_token}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        yield c


# --------------------------------------------------------------------------- #
# Outline → draft
# --------------------------------------------------------------------------- #
def test_outline_parser_strips_bullets_nests_by_indent_and_dedupes_keys():
    items = outline_to_syllabus(
        "Week 1: Replication\n"
        "  - Leader election\n"
        "  - Quorums:\n"
        "      1. Read quorums\n"
        "\n"
        "* Consistency\n"
        "- Quorums\n"
    )
    got = [(i.concept_key, i.label, i.parent, i.source.value) for i in items]
    assert got == [
        ("week-1-replication", "Week 1: Replication", None, "user"),
        ("leader-election", "Leader election", "week-1-replication", "user"),
        ("quorums", "Quorums", "week-1-replication", "user"),
        ("read-quorums", "Read quorums", "quorums", "user"),
        ("consistency", "Consistency", None, "user"),
        ("quorums-2", "Quorums", None, "user"),
    ]
    assert outline_to_syllabus("  \n\n") == []


async def test_outline_creates_a_draft_the_next_sync_refines(api):
    outline = "Replication\n  Leader election\n  Quorums\nConsistency models"
    created = await ok(
        await api.post(
            "/v1/tracks", json={"name": " Systems ", "kind": "notes", "outline": outline}
        )
    )
    assert created["name"] == "Systems" and created["kind"] == "notes"
    agenda = created["agenda"]
    assert (agenda["status"], agenda["source"], agenda["outline"]) == ("draft", "user", outline)
    assert [(i["concept_key"], i["parent"], i["source"]) for i in agenda["syllabus"]] == [
        ("replication", None, "user"),
        ("leader-election", "replication", "user"),
        ("quorums", "replication", "user"),
        ("consistency-models", None, "user"),
    ]
    assert created["archived"] is False and created["concept_count"] == 0

    # The sync sees the draft and its outline, and refines it.
    (track,) = (await call("jolt_get_tracks"))["tracks"]
    assert track["agenda"]["status"] == "draft" and track["agenda"]["outline"] == outline
    assert track["kind"] == "notes" and track["archived"] is False
    refined = await call(
        "jolt_set_agenda",
        track_id=created["id"],
        syllabus=[
            {"concept_key": "replication", "label": "Replication"},
            {"concept_key": "leader-election", "label": "Leader election", "parent": "replication"},
            {"concept_key": "raft", "label": "Raft", "parent": "leader-election"},
        ],
        mark_refined=True,
    )
    assert refined["status"] == "refined" and refined["source"] == "agent"
    # Items the learner wrote keep their provenance; the agent's addition is "agent".
    assert [i["source"] for i in refined["syllabus"]] == ["user", "user", "agent"]
    detail = await ok(await api.get(f"/v1/tracks/{created['id']}"))
    assert detail["agenda"]["status"] == "refined"
    assert [(i["concept_key"], i["state"], i["source"]) for i in detail["items"]] == [
        ("replication", "not_covered", "user"),
        ("leader-election", "not_covered", "user"),
        ("raft", "not_covered", "agent"),
    ]


async def test_create_track_validation(api):
    assert (await api.post("/v1/tracks", json={"name": "  "})).status_code == 422
    assert (await api.post("/v1/tracks", json={"name": "x" * 121})).status_code == 422
    both = await api.post("/v1/tracks", json={"name": "A", "outline": "a", "syllabus": ["b"]})
    assert both.status_code == 422
    dsa = await api.post("/v1/tracks", json={"name": "A", "kind": "dsa_patterns"})
    assert dsa.status_code == 422
    # A syllabus is still a user-authored refined agenda; nothing is still none.
    labels = await ok(await api.post("/v1/tracks", json={"name": "B", "syllabus": ["x"]}))
    assert labels["agenda"]["status"] == "refined"
    assert labels["agenda"]["syllabus"][0]["source"] == "user"
    empty = await ok(await api.post("/v1/tracks", json={"name": "C", "outline": "   "}))
    assert empty["agenda"]["status"] == "none" and empty["agenda"]["outline"] is None


# --------------------------------------------------------------------------- #
# Rename, archive
# --------------------------------------------------------------------------- #
async def test_rename_and_archive_pause_the_track(api, runtime, mcp_user):
    tid = await new_track(api, "Sytems")
    cid, _ = await new_concept(tid, "Quorums")
    other = await new_track(api, "Biology")
    await new_concept(other, "Mitosis")

    renamed = await ok(await api.patch(f"/v1/tracks/{tid}", json={"name": "Systems"}))
    assert renamed["name"] == "Systems" and renamed["due_count"] == 1
    assert (await api.patch(f"/v1/tracks/{tid}", json={"name": " "})).status_code == 422

    archived = await ok(await api.patch(f"/v1/tracks/{tid}", json={"archived": True}))
    assert archived["archived"] is True and archived["archived_at"]
    assert archived["due_count"] == 0 and archived["concept_count"] == 1
    listed = {t["id"]: t for t in await ok(await api.get("/v1/tracks"))}
    assert listed[tid]["archived"] is True  # still listed, flagged
    session = await ok(await api.post("/v1/review-sessions", json={}))
    assert [i["track_id"] for i in session["items"]] == [other]  # paused track left out
    detail = await ok(await api.get(f"/v1/tracks/{tid}"))
    assert detail["due_today"] == [] and detail["items"][0]["concept_id"] == cid

    back = await ok(await api.patch(f"/v1/tracks/{tid}", json={"archived": False}))
    assert back["archived"] is False and back["archived_at"] is None and back["due_count"] == 1


# --------------------------------------------------------------------------- #
# Tracks list counts
# --------------------------------------------------------------------------- #
async def test_track_list_counts(api, runtime, mcp_user):
    tid = await new_track(api, "Systems")
    sid = await upload(api, runtime, tid)
    await upload(api, runtime, tid, title="Lecture 5", data=b"\xff\xd8 p2")
    await read_source(sid)
    due, _ = await new_concept(tid, "Quorums", source_id=sid)
    learning, _ = await new_concept(tid, "Raft")
    mastered, _ = await new_concept(tid, "Paxos")
    await new_concept(tid, "Unanswerable", questions=0)  # due, but nothing to ask

    now = datetime.now(timezone.utc)
    await set_card(
        runtime,
        mcp_user.user_id,
        learning,
        reps=1,
        stability=10.0,
        last_review_at=now - timedelta(days=2),
        due_at=now + timedelta(days=5),
    )
    await set_card(
        runtime,
        mcp_user.user_id,
        mastered,
        reps=4,
        stability=40.0,
        last_review_at=now - timedelta(days=1),
        due_at=now + timedelta(days=40),
        mastered_at=now - timedelta(days=1),
    )

    (track,) = await ok(await api.get("/v1/tracks"))
    assert track["id"] == tid
    assert (track["concept_count"], track["due_count"], track["source_count"]) == (4, 1, 2)
    assert track["mastered_count"] == 1
    assert 0.9 < track["retention"] < 1.0  # mean predicted recall of the two reviewed cards
    assert track["description"] is None and track["enrolled"] is False
    assert track["kind"] == "notes" and track["origin"] == "user"
    assert due  # the due concept is the one with a question and no review


# --------------------------------------------------------------------------- #
# Track detail
# --------------------------------------------------------------------------- #
async def test_track_detail_items_states_due_today_and_recent_sources(api, runtime, mcp_user):
    tid = await new_track(api, "Systems", syllabus=["hashing", "quorums", "raft", "paxos"])
    sid = await upload(api, runtime, tid, title="12 Sep notes")
    await read_source(sid)
    hashing, (hq,) = await new_concept(tid, "Consistent hashing", key="hashing", source_id=sid)
    quorums, _ = await new_concept(tid, "Quorums", key="quorums", questions=2)
    raft, _ = await new_concept(tid, "Raft", key="raft")
    stray, _ = await new_concept(tid, "Gossip")  # no key: not filed under any item

    # Answer hashing once (learning, with a last answer); make raft mastered.
    session = await ok(await api.post("/v1/review-sessions", json={"concept_id": hashing}))
    await answer(api, session["items"][0], text="keys on a ring")
    now = datetime.now(timezone.utc)
    await set_card(
        runtime,
        mcp_user.user_id,
        raft,
        reps=3,
        stability=30.0,
        last_review_at=now,
        due_at=now + timedelta(days=30),
        mastered_at=now,
    )
    await make_due(runtime, mcp_user.user_id, hashing)

    detail = await ok(await api.get(f"/v1/tracks/{tid}"))
    assert detail["agenda"]["status"] == "refined"
    rows = {(i["concept_key"], i["concept_id"]): i for i in detail["items"]}
    assert [i["concept_key"] for i in detail["items"]] == [
        "hashing",
        "quorums",
        "raft",
        "paxos",
        stray,  # an unfiled concept is listed by its id
    ]
    h = rows[("hashing", hashing)]
    assert h["state"] == "due" and h["question_count"] == 1 and h["question_version"] == 1
    assert h["source"] == "user" and h["off_plan"] is False
    assert h["concept_title"] == "Consistent hashing" and h["label"] == "hashing"
    assert h["found_in"]["source_id"] == sid and h["found_in"]["source_title"] == "12 Sep notes"
    assert h["next_review_at"]
    q = rows[("quorums", quorums)]
    assert q["state"] == "new" and q["question_count"] == 2 and q["found_in"] is None
    r = rows[("raft", raft)]
    assert r["state"] == "mastered" and r["next_review_at"] is None  # retired (setting on)
    p = rows[("paxos", None)]
    assert p["state"] == "not_covered" and p["question_count"] == 0
    g = rows[(stray, stray)]
    assert g["label"] == "Gossip" and g["source"] == "emergent" and g["off_plan"] is False

    due_ids = [d["concept_id"] for d in detail["due_today"]]
    assert set(due_ids) == {hashing, quorums, stray}
    by_id = {d["concept_id"]: d for d in detail["due_today"]}
    assert by_id[hashing]["question_id"] == hq
    assert by_id[hashing]["last_answer"] == "keys on a ring"
    assert by_id[quorums]["last_answer"] is None and by_id[quorums]["title"] == "Quorums"
    assert detail["recent_sources"] == [
        {
            "id": sid,
            "title": "12 Sep notes",
            "kind": "photos",
            "page_count": 1,
            "uploaded_at": detail["recent_sources"][0]["uploaded_at"],
        }
    ]
    assert (detail["concept_count"], detail["mastered_count"], detail["source_count"]) == (4, 1, 1)
    assert (await api.get("/v1/tracks/nope")).status_code == 404


async def test_due_today_serves_the_same_question_as_the_queue(api, runtime, mcp_user):
    tid = await new_track(api)
    cid, qids = await new_concept(tid, "Quorums", questions=3)
    session = await ok(await api.post("/v1/review-sessions", json={}))
    await answer(api, session["items"][0])
    await make_due(runtime, mcp_user.user_id, cid)
    detail = await ok(await api.get(f"/v1/tracks/{tid}"))
    queue = await ok(await api.post("/v1/review-sessions", json={}))
    assert detail["due_today"][0]["question_id"] == queue["items"][0]["question_id"] == qids[1]


async def test_off_plan_concepts_on_a_locked_track(api, runtime, mcp_user):
    tid = await new_track(api, "Systems", syllabus=["hashing"])
    covered, _ = await new_concept(tid, "Consistent hashing", key="hashing")
    await ok(await api.post(f"/v1/tracks/{tid}/agenda/lock"))
    off, _ = await new_concept(tid, "Vector clocks", key="vector-clocks")

    agenda = await call("jolt_get_agenda", track_id=tid)
    assert [i["concept_key"] for i in agenda["syllabus"]] == ["hashing"]  # not auto-added

    detail = await ok(await api.get(f"/v1/tracks/{tid}"))
    rows = {i["concept_id"]: i for i in detail["items"]}
    assert rows[off]["off_plan"] is True and rows[off]["concept_key"] == "vector-clocks"
    assert rows[off]["state"] == "new"  # kept and reviewed as usual
    assert rows[covered]["off_plan"] is False
    assert detail["off_plan_count"] == 1 and detail["concept_count"] == 2

    cov = await ok(await api.get(f"/v1/tracks/{tid}/coverage"))
    assert (cov["total_concepts"], cov["off_plan_concepts"]) == (1, 1)
    assert cov["coverage_ratio"] == 1.0
    mcp_cov = await call("jolt_get_coverage", track_id=tid)
    assert mcp_cov["off_plan_concepts"] == 1 and mcp_cov["total_concepts"] == 1

    # Unlocked again, the concept is simply not filed yet (no longer off-plan).
    await ok(await api.post(f"/v1/tracks/{tid}/agenda/unlock"))
    detail = await ok(await api.get(f"/v1/tracks/{tid}"))
    assert {i["concept_id"]: i for i in detail["items"]}[off]["off_plan"] is False


async def test_auto_added_items_are_emergent(api):
    tid = await new_track(api, "Systems", syllabus=["hashing"])
    await new_concept(tid, "Raft", key="raft")
    detail = await ok(await api.get(f"/v1/tracks/{tid}"))
    assert [(i["concept_key"], i["source"]) for i in detail["items"]] == [
        ("hashing", "user"),
        ("raft", "emergent"),
    ]


async def test_legacy_cards_are_backfilled_on_first_read(api, runtime, mcp_user):
    tid = await new_track(api)
    cid, (qid,) = await new_concept(tid, "Quorums")
    await set_card(runtime, mcp_user.user_id, cid, active_question_ids=None, question_version=None)
    detail = await ok(await api.get(f"/v1/tracks/{tid}"))
    assert detail["items"][0]["question_count"] == 1
    assert detail["due_today"][0]["question_id"] == qid
    card = await runtime.repos.concept_states.for_concept(mcp_user.user_id, cid)
    assert card.active_question_ids == [qid] and card.question_version == 1


# --------------------------------------------------------------------------- #
# Concept detail
# --------------------------------------------------------------------------- #
async def test_concept_detail_timeline_versions_adaptive_note_fsrs(api, runtime, mcp_user):
    tid = await new_track(api, "Systems")
    sid = await upload(api, runtime, tid, title="12 Sep notes")
    await read_source(sid)
    cid, (q1,) = await new_concept(tid, "Consistent hashing", source_id=sid, page=2)
    await ok(await api.patch("/v1/settings", json={"allow_question_rewrite": True}))

    fresh = await ok(await api.get(f"/v1/concepts/{cid}"))
    assert fresh["state"] == "new" and fresh["timeline"] == []
    assert fresh["summary"] == "Consistent hashing explained."
    assert fresh["found_in"]["page"] == 2 and fresh["found_in"]["source_title"] == "12 Sep notes"
    assert fresh["current_question"]["question_id"] == q1 and fresh["adaptive_note"] is None

    session = await ok(await api.post("/v1/review-sessions", json={}))
    s1, _ = await answer(api, session["items"][0], text=LONG, latency=5_000)
    pending = await call("jolt_get_pending_gradings")
    await call(
        "jolt_submit_gradings",
        gradings=[
            {
                "review_id": s1["review_id"],
                "suggested_fsrs_grade": 3,
                "rationale": "You named the ring and the clockwise rule.",
                "rewritten_question": {
                    **question("What moves when a node joins the ring?"),
                    "rewrite_reason": "You know the basics; this probes rebalancing.",
                },
            }
        ],
    )
    assert pending["gradings"][0]["review_id"] == s1["review_id"]
    await make_due(runtime, mcp_user.user_id, cid)
    session = await ok(await api.post("/v1/review-sessions", json={}))
    await answer(api, session["items"][0], correct=False, text="not sure")

    d = await ok(await api.get(f"/v1/concepts/{cid}"))
    assert (d["id"], d["track_id"], d["track_name"]) == (cid, tid, "Systems")
    assert d["title"] == "Consistent hashing" and d["status"] == "active"
    assert d["retired"] is False and d["off_plan"] is False and d["successor_id"] is None
    first, second = d["timeline"]
    assert first["free_text"] == LONG and first["is_correct"] is True
    assert (first["grade"], first["grading"]) == (3, "graded")
    assert first["feedback"] == "You named the ring and the clockwise rule."
    assert (first["question_id"], first["question_version"]) == (q1, 1)
    assert first["interval_days"] is None
    assert second["free_text"] == "not sure" and second["is_correct"] is False
    assert (second["grade"], second["grading"], second["feedback"]) == (1, "provisional", None)
    assert second["question_version"] == 2 and second["interval_days"] == 0
    assert [v["version"] for v in d["question_versions"]] == [1, 2]
    assert d["current_question"]["version"] == 2
    assert d["adaptive_note"] == "You know the basics; this probes rebalancing."
    fsrs = d["fsrs"]
    assert fsrs["reps"] == 2 and fsrs["lapses"] == 1 and fsrs["due_at"]
    assert 0 < fsrs["retrievability"] <= 1 and fsrs["stability"] > 0
    assert d["state"] in {"due", "learning"}
    assert (await api.get("/v1/concepts/nope")).status_code == 404


# --------------------------------------------------------------------------- #
# Corrections
# --------------------------------------------------------------------------- #
async def test_correction_note_reaches_the_next_sync(api, runtime, mcp_user):
    tid = await new_track(api)
    cid, _ = await new_concept(tid, "Quorums")
    queued = await ok(
        await api.post(f"/v1/concepts/{cid}/correction", json={"note": " W + R > N, not >= "})
    )
    assert queued["status"] == "queued" and queued["note"] == "W + R > N, not >="
    assert queued["concept_id"] == cid and queued["created_at"]
    assert (await api.post(f"/v1/concepts/{cid}/correction", json={"note": ""})).status_code == 422
    assert (
        await api.post(f"/v1/concepts/{cid}/correction", json={"note": "   "})
    ).status_code == 422
    await ok(await api.post(f"/v1/concepts/{cid}/correction", json={"note": "Also: typo"}))

    detail = await ok(await api.get(f"/v1/concepts/{cid}"))
    assert [n["note"] for n in detail["pending_corrections"]] == ["W + R > N, not >=", "Also: typo"]

    plan = await call("jolt_sync")
    assert plan["pending_corrections"] == 1
    (pending,) = (await call("jolt_get_pending_corrections"))["corrections"]
    assert (pending["concept_id"], pending["track_id"], pending["title"]) == (cid, tid, "Quorums")
    assert pending["text"] == "Quorums explained."
    assert [n["note"] for n in pending["notes"]] == ["W + R > N, not >=", "Also: typo"]

    fixed = await call(
        "jolt_correct_concept", track_id=tid, concept_id=cid, text="Quorums: W + R > N."
    )
    assert fixed == {"concept_id": cid, "updated": True, "corrections_cleared": 2}
    assert (await call("jolt_sync"))["pending_corrections"] == 0
    assert (await call("jolt_get_pending_corrections"))["corrections"] == []
    detail = await ok(await api.get(f"/v1/concepts/{cid}"))
    assert detail["summary"] == "Quorums: W + R > N." and detail["pending_corrections"] == []
    wrong_track = await call("jolt_correct_concept", track_id="other", concept_id=cid, text="x")
    assert wrong_track == {"error": "concept not found"}


# --------------------------------------------------------------------------- #
# No N+1: the number of Cosmos calls does not grow with the screen's size
# --------------------------------------------------------------------------- #
async def _populate(api, runtime, tid: str, n: int) -> list[str]:
    sid = await upload(api, runtime, tid, title=f"notes {n}", data=f"page {n}".encode())
    await read_source(sid)
    ids = []
    for i in range(n):
        cid, _ = await new_concept(tid, f"Concept {n}-{i}", key=f"k{i}", source_id=sid)
        ids.append(cid)
    return ids


async def _calls(monkeypatch, api, path: str) -> int:
    """Cosmos calls one GET makes, besides authentication (token + user)."""
    with count_cosmos_calls(monkeypatch) as counts:
        await ok(await api.get(path))
    return sum(n for (_, container), n in counts.items() if container not in {"tokens", "users"})


async def test_screens_cost_a_fixed_number_of_cosmos_calls(api, runtime, mcp_user, monkeypatch):
    small = await new_track(api, "Small")
    big = await new_track(api, "Big")
    small_ids = await _populate(api, runtime, small, 2)
    big_ids = await _populate(api, runtime, big, 12)
    for cid in small_ids[:1] + big_ids[:6]:  # history on both, more on the big one
        session = await ok(await api.post("/v1/review-sessions", json={"concept_id": cid}))
        await answer(api, session["items"][0])

    # Track detail: same cost for 2 and 12 concepts.
    small_detail = await _calls(monkeypatch, api, f"/v1/tracks/{small}")
    big_detail = await _calls(monkeypatch, api, f"/v1/tracks/{big}")
    assert small_detail == big_detail
    assert big_detail == 4, big_detail  # track, cards on it, its concepts, sources

    # Tracks: same cost for 2 tracks and for 6.
    two = await _calls(monkeypatch, api, "/v1/tracks")
    for i in range(4):
        await _populate(api, runtime, await new_track(api, f"More {i}"), 3)
    six = await _calls(monkeypatch, api, "/v1/tracks")
    assert two == six == 4, (two, six)  # own tracks, curated tracks, cards, sources

    # Concept: same cost with no answers and with one.
    plain = await _calls(monkeypatch, api, f"/v1/concepts/{big_ids[-1]}")
    answered = await _calls(monkeypatch, api, f"/v1/concepts/{big_ids[0]}")
    # card, concept, track, question versions, reviews, source
    assert plain == answered == 6, (plain, answered)
