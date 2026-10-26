"""BE-07 §6: supersede reconciliation with a ceiling and a resumable checkpoint.

`jolt_store_extraction(concepts=...)` classifies a re-read's concepts against
the previous read's and applies each class; a supersede larger than
`RECONCILE_MAX_CONCEPTS` is applied in batches resumed by
`jolt_continue_reconciliation`, and a crash mid-step converges on resume.
"""

from __future__ import annotations

import httpx
import pytest

from jolt.main import create_app
from jolt.services.reconcile import ReconcileService
from tests.journeys import TOOLS, answer, call, new_track, ok, question, read_source, upload

HASHING = (
    "Consistent hashing places keys and nodes on a ring; a key belongs to the next node "
    "clockwise, so adding a node moves only the keys between it and its predecessor."
)
QUORUM = "A read quorum R and a write quorum W must overlap: R + W > N."
RAFT = "Raft elects a leader per term; a candidate needs votes from a majority."
GOSSIP = "Gossip protocols spread state by random pairwise exchanges."
CRDT = "CRDTs merge concurrent updates without coordination."


@pytest.fixture
async def api(settings, runtime, mcp_user):
    app = create_app(settings=settings, runtime=runtime)
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {mcp_user.app_token}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        yield c


async def first_read(api, runtime, concepts: list[dict]) -> tuple[str, str, dict]:
    tid = await new_track(api, "Systems")
    sid = await upload(api, runtime, tid)
    stored = await read_source(sid, concepts=concepts)
    return tid, sid, stored


async def reread(api, sid: str, concepts: list[dict]) -> dict:
    await ok(await api.post(f"/v1/sources/{sid}/reread", json={}))
    return await read_source(sid, markdown="# Read 2", supersede=True, concepts=concepts)


async def test_first_read_with_concepts_creates_them(api, runtime, mcp_user):
    tid, sid, stored = await first_read(
        api, runtime, [{"text": HASHING, "title": "Hashing", "concept_key": "hashing", "page": 1}]
    )
    rec = stored["reconciliation"]
    assert rec["status"] == "complete" and rec["total_steps"] == rec["applied_steps"] == 1
    assert [d["change"] for d in rec["deltas"]] == ["new"]
    (cid,) = rec["concept_ids"]
    detail = await ok(await api.get(f"/v1/concepts/{cid}"))
    assert detail["found_in"]["page"] == 1 and detail["concept_key"] == "hashing"
    source = await ok(await api.get(f"/v1/sources/{sid}"))
    assert source["active_read"]["concepts"] == [
        {"concept_id": cid, "title": "Hashing", "is_new": True}
    ]
    agenda = await call("jolt_get_agenda", track_id=tid)
    assert [(i["concept_key"], i["source"]) for i in agenda["syllabus"]] == [
        ("hashing", "emergent")
    ]


async def test_supersede_applies_every_change_class(api, runtime, mcp_user):
    tid, sid, stored = await first_read(
        api,
        runtime,
        [
            {"text": HASHING, "title": "Hashing"},
            {"text": QUORUM, "title": "Quorums"},
            {"text": RAFT, "title": "Raft"},
            {"text": GOSSIP, "title": "Gossip"},
        ],
    )
    hashing, quorum, raft, gossip = stored["reconciliation"]["concept_ids"]
    for cid in (hashing, quorum, raft, gossip):
        await call("jolt_create_questions", concept_id=cid, questions=[question(f"{cid}?")])
    # Give the soon-to-change concept some memory to carry forward.
    session = await ok(await api.post("/v1/review-sessions", json={"concept_id": raft}))
    await answer(api, session["items"][0])
    old_card = await runtime.repos.concept_states.for_concept(mcp_user.user_id, raft)
    assert old_card.reps == 1

    preview = await call(
        "jolt_diff_extractions",
        track_id=tid,
        proposed_concepts=[{"text": HASHING}, {"text": QUORUM + " Always."}],
    )
    assert [d["change"] for d in preview["deltas"]][:2] == ["unchanged", "refined"]

    stored = await reread(
        api,
        sid,
        [
            {"text": HASHING, "title": "Hashing"},  # unchanged
            {"text": QUORUM + " Always.", "title": "Quorums"},  # refined
            {"text": "Raft elects a leader; votes from a majority of servers.", "title": "Raft"},
            {"text": CRDT, "title": "CRDTs", "concept_key": "crdt"},  # new
        ],  # gossip: removed
    )
    rec = stored["reconciliation"]
    assert rec["status"] == "complete" and rec["remaining_steps"] == 0
    changes = {d["existing_concept_id"]: d for d in rec["deltas"] if d["existing_concept_id"]}
    assert changes[hashing]["change"] == "unchanged" and changes[hashing]["concept_id"] == hashing
    assert changes[quorum]["change"] == "refined" and changes[quorum]["concept_id"] == quorum
    assert changes[raft]["change"] == "changed"
    successor = changes[raft]["concept_id"]
    assert successor not in {raft, hashing, quorum, gossip}
    assert changes[gossip]["change"] == "removed" and changes[gossip]["concept_id"] is None
    new_id = rec["concept_ids"][3]
    assert rec["concept_ids"] == [hashing, quorum, successor, new_id]
    assert [d["index"] for d in rec["deltas"]] == [0, 1, 2, 3, None]

    # refined: text updated in place, questions kept
    refined = await ok(await api.get(f"/v1/concepts/{quorum}"))
    assert refined["summary"] == QUORUM + " Always." and len(refined["question_versions"]) == 1
    # changed: the old concept is superseded, its questions retired, its card out of the queue
    old = await ok(await api.get(f"/v1/concepts/{raft}"))
    assert old["status"] == "superseded" and old["successor_id"] == successor
    assert old["current_question"] is None and old["fsrs"]["due_at"] is None
    assert all(not v["active"] for v in old["question_versions"])
    assert len(old["timeline"]) == 1  # its history stays with it
    # ... and the successor carries the memory state forward
    new_card = await runtime.repos.concept_states.for_concept(mcp_user.user_id, successor)
    assert new_card.carried_from == raft
    assert (new_card.reps, new_card.stability) == (old_card.reps, old_card.stability)
    assert new_card.due_at == old_card.due_at
    # removed: superseded, no successor
    gone = await ok(await api.get(f"/v1/concepts/{gossip}"))
    assert gone["status"] == "superseded" and gone["successor_id"] is None

    # Past reads credit the new read; only the new concept is new.
    source = await ok(await api.get(f"/v1/sources/{sid}"))
    flags = {c["concept_id"]: c["is_new"] for c in source["active_read"]["concepts"]}
    assert flags == {hashing: False, quorum: False, successor: False, new_id: True}
    assert source["reads"][1]["status"] == "superseded"

    # Track detail lists the live concepts only; the new key was auto-added.
    detail = await ok(await api.get(f"/v1/tracks/{tid}"))
    listed = {i["concept_id"] for i in detail["items"] if i["concept_id"]}
    assert listed == {hashing, quorum, successor, new_id}
    assert detail["concept_count"] == 4
    assert "crdt" in {i["concept_key"] for i in detail["agenda"]["syllabus"]}

    # The successor needs questions; answering it continues from the carried card.
    await call("jolt_create_questions", concept_id=successor, questions=[question("Raft?")])
    session = await ok(await api.post("/v1/review-sessions", json={"concept_id": successor}))
    await answer(api, session["items"][0])
    folded = await runtime.repos.concept_states.for_concept(mcp_user.user_id, successor)
    assert folded.reps == old_card.reps + 1
    # Superseded concepts never come back into a session.
    queue = await ok(await api.post("/v1/review-sessions", json={}))
    assert not {raft, gossip} & {i["concept_id"] for i in queue["items"]}
    assert (await ok(await api.post("/v1/review-sessions", json={"concept_id": gossip})))[
        "items"
    ] == []


async def test_large_supersede_is_checkpointed_and_resumed(api, runtime, monkeypatch):
    monkeypatch.setattr(runtime.settings, "reconcile_max_concepts", 3)
    topics = [f"Topic number {i}: " + "x" * i * 7 + " distinct body " + str(i) for i in range(8)]
    tid, sid, stored = await first_read(api, runtime, [{"text": t} for t in topics])
    rec = stored["reconciliation"]
    assert (rec["status"], rec["applied_steps"], rec["remaining_steps"]) == ("partial", 3, 5)
    assert rec["max_steps_per_call"] == 3 and len(rec["concept_ids"]) == 8
    applied = [d["applied"] for d in rec["deltas"]]
    assert applied == [True] * 3 + [False] * 5
    # A concept not applied yet does not exist: questions for it are refused.
    with pytest.raises(Exception):
        await TOOLS["jolt_create_questions"](
            concept_id=rec["concept_ids"][7], questions=[question("early?")]
        )

    rec = await call("jolt_continue_reconciliation", source_id=sid)
    assert (rec["status"], rec["applied_steps"]) == ("partial", 6)
    rec = await call("jolt_continue_reconciliation", source_id=sid)
    assert (rec["status"], rec["applied_steps"], rec["remaining_steps"]) == ("complete", 8, 0)
    again = await call("jolt_continue_reconciliation", source_id=sid)
    assert again == rec  # complete: unchanged, nothing re-applied
    detail = await ok(await api.get(f"/v1/tracks/{tid}"))
    assert detail["concept_count"] == 8
    source = await ok(await api.get(f"/v1/sources/{sid}"))
    assert source["concepts_found"] == 8


async def test_a_step_interrupted_mid_way_converges_on_resume(api, runtime, mcp_user, monkeypatch):
    tid, sid, stored = await first_read(api, runtime, [{"text": RAFT}, {"text": GOSSIP}])
    raft, gossip = stored["reconciliation"]["concept_ids"]

    # Crash after the first step's writes but before its checkpoint is saved.
    real_advance = ReconcileService._advance
    crashed = {"n": 0}

    async def flaky_advance(self, *args, **kwargs):
        if crashed["n"] == 0:
            crashed["n"] += 1
            raise RuntimeError("container killed")
        return await real_advance(self, *args, **kwargs)

    monkeypatch.setattr(ReconcileService, "_advance", flaky_advance)
    changed = "Raft elects one leader per term with a majority vote of the cluster."
    with pytest.raises(RuntimeError):
        await reread(api, sid, [{"text": changed}, {"text": CRDT}])
    rec = await call("jolt_continue_reconciliation", source_id=sid)
    assert rec["status"] == "complete" and rec["applied_steps"] == rec["total_steps"]

    successor = rec["concept_ids"][0]
    concepts = runtime.cosmos.container("concepts").all_items()
    assert sum(1 for c in concepts if c["id"] == successor) == 1  # created once
    cards = runtime.cosmos.container("concept_states").all_items()
    assert sum(1 for c in cards if c["concept_id"] == successor) == 1
    live = {c["id"] for c in concepts if c["status"] == "active"}
    assert live == {successor, rec["concept_ids"][1]}
    source = await ok(await api.get(f"/v1/sources/{sid}"))
    assert len(source["active_read"]["concepts"]) == 2  # credited once each


async def test_invalid_concepts_store_nothing(api, runtime):
    tid = await new_track(api)
    sid = await upload(api, runtime, None)  # no track on the source
    await call("jolt_get_unprocessed_sources")
    no_track = await call(
        "jolt_store_extraction", source_id=sid, markdown="x", concepts=[{"text": "a"}]
    )
    assert no_track == {"error": "each concept needs a track_id (the source has no track)"}
    blank = await call(
        "jolt_store_extraction",
        source_id=sid,
        markdown="x",
        concepts=[{"text": " ", "track_id": tid}],
    )
    assert blank == {"error": "each concept needs a non-empty text"}
    assert await runtime.repos.extractions.history_for_source(sid) == []
    missing = await call("jolt_continue_reconciliation", source_id=sid)
    assert "no reconciliation" in missing["error"]


async def test_supersede_without_concepts_moves_the_pointer_only(api, runtime):
    tid, sid, stored = await first_read(api, runtime, [{"text": HASHING}])
    (cid,) = stored["reconciliation"]["concept_ids"]
    await ok(await api.post(f"/v1/sources/{sid}/reread", json={}))
    plain = await read_source(sid, markdown="# Read 2", supersede=True)
    assert plain["reconciliation"] is None
    assert (await ok(await api.get(f"/v1/concepts/{cid}")))["status"] == "active"
    missing = await call("jolt_continue_reconciliation", source_id=sid)
    assert "no reconciliation" in missing["error"]
