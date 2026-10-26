"""BE-09: curated tracks, enrolment, the DSA import and DSA sessions, over the
in-memory fakes (A8).

The DSA content comes from a small synthetic Jolt checkout (`tests/dsa_fixture`);
one test imports the real Jolt repo when it sits next to this one.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from jolt.curated.dsa_import import (
    DSA_TRACK_ID,
    ImportRefused,
    concept_id_for,
    import_dsa,
    question_id_for,
)
from jolt.curated.dsa_source import load_bundle, pattern_slug
from jolt.curated.ts_literal import LiteralError, read_export
from jolt.domain import dsa
from jolt.main import create_app
from jolt.services.sync import SyncError, SyncService
from tests import dsa_fixture as fx
from tests.journeys import call, new_concept, new_track, ok

JOLT_REPO = Path(__file__).resolve().parents[2] / "Jolt"


@pytest.fixture
def jolt_repo(tmp_path: Path) -> Path:
    return fx.write_repo(tmp_path / "Jolt")


@pytest.fixture
async def imported(runtime, jolt_repo):
    return await import_dsa(runtime, load_bundle(jolt_repo))


@pytest.fixture
async def api(settings, runtime, mcp_user, imported):
    app = create_app(settings=settings, runtime=runtime)
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {mcp_user.app_token}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        yield c


async def enrol(api) -> dict:
    return await ok(await api.post(f"/v1/tracks/{DSA_TRACK_ID}/enrol"))


async def dsa_session(api, **body) -> dict:
    return await ok(await api.post("/v1/review-sessions", json={"kind": "dsa_patterns", **body}))


def problem_of(question_id: str) -> tuple:
    for row in fx.PROBLEMS:
        if question_id_for(DSA_TRACK_ID, row[0]) == question_id:
            return row
    raise AssertionError(f"unknown question {question_id}")


async def answer_with(api, item: dict, pattern: str) -> tuple[dict, dict]:
    s1 = await ok(
        await api.post(
            f"/v1/reviews/{item['question_id']}/stage1",
            json={"concept_id": item["concept_id"], "free_text": "", "session_id": None},
        )
    )
    s2 = await ok(
        await api.post(
            f"/v1/reviews/{s1['review_id']}/stage2",
            json={"selected_index": s1["options"].index(pattern)},
        )
    )
    return s1, s2


# --------------------------------------------------------------------------- #
# Catalog, enrolment, read-only
# --------------------------------------------------------------------------- #
async def test_catalog_enrol_unenrol_and_onboarding(api):
    me = await ok(await api.get("/v1/me"))
    assert me["onboarding"]["has_track"] is False

    (track,) = await ok(await api.get("/v1/tracks/catalog"))
    assert track["id"] == DSA_TRACK_ID
    assert track["origin"] == "jolt" and track["user_id"] is None
    assert track["kind"] == "dsa_patterns" and track["agenda"]["status"] == "locked"
    assert track["description"].startswith("The 24 patterns")
    assert track["enrolled"] is False and track["due_count"] == 0
    assert track["syllabus"] == fx.PATTERNS

    first = await enrol(api)
    assert first["track_id"] == DSA_TRACK_ID and first["enrolled"] is True
    again = await enrol(api)
    assert again["enrolled_at"] == first["enrolled_at"]  # idempotent

    (track,) = await ok(await api.get("/v1/tracks/catalog"))
    assert track["enrolled"] is True
    listed = {t["id"]: t for t in await ok(await api.get("/v1/tracks"))}
    assert listed[DSA_TRACK_ID]["enrolled"] is True
    assert (await ok(await api.get("/v1/me")))["onboarding"]["has_track"] is True

    assert (await api.delete(f"/v1/tracks/{DSA_TRACK_ID}/enrol")).status_code == 204
    assert (await api.delete(f"/v1/tracks/{DSA_TRACK_ID}/enrol")).status_code == 204
    (track,) = await ok(await api.get("/v1/tracks/catalog"))
    assert track["enrolled"] is False
    assert (await ok(await api.get("/v1/me")))["onboarding"]["has_track"] is False


async def test_enrol_refuses_anything_but_a_curated_track(api):
    own = await new_track(api, "Mine")
    for track_id in (own, "nope"):
        resp = await api.post(f"/v1/tracks/{track_id}/enrol")
        assert resp.status_code == 404 and resp.json()["detail"] == "curated track not found"
        assert (await api.delete(f"/v1/tracks/{track_id}/enrol")).status_code == 404
    listed = {t["id"]: t for t in await ok(await api.get("/v1/tracks"))}
    assert listed[own]["enrolled"] is False


async def test_curated_track_is_read_only(api, runtime, mcp_user):
    await enrol(api)
    assert (await api.patch(f"/v1/tracks/{DSA_TRACK_ID}", json={"name": "x"})).status_code == 404
    assert (await api.post(f"/v1/tracks/{DSA_TRACK_ID}/agenda/unlock")).status_code == 404
    resp = await api.post("/v1/tracks", json={"name": "My DSA", "kind": "dsa_patterns"})
    assert resp.status_code == 422
    agenda = await call("jolt_set_agenda", track_id=DSA_TRACK_ID, syllabus=[])
    assert agenda == {"error": "track not found"}

    # Even after a first review gives the learner a card on a pattern, its
    # questions and text stay Jolt's: the agent tools refuse to write them.
    session = await dsa_session(api)
    item = session["items"][0]
    _, s2 = await answer_with(api, item, "Stack")
    with pytest.raises(SyncError):
        await SyncService(runtime).create_questions(mcp_user.user_id, s2["concept_id"], [])
    fixed = await call(
        "jolt_correct_concept", track_id=DSA_TRACK_ID, concept_id=s2["concept_id"], text="x"
    )
    assert fixed == {"error": "concept not found"}
    resp = await api.post(f"/v1/concepts/{s2['concept_id']}/correction", json={"note": "typo"})
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Track detail, library, decision tree, concept
# --------------------------------------------------------------------------- #
async def test_track_detail_lists_every_pattern_new_until_reviewed(api):
    detail = await ok(await api.get(f"/v1/tracks/{DSA_TRACK_ID}"))
    assert [i["label"] for i in detail["items"]] == fx.PATTERNS
    assert {i["state"] for i in detail["items"]} == {"new"}
    counts = {i["label"]: i["question_count"] for i in detail["items"]}
    assert counts == dict(Counter(row[2] for row in fx.PROBLEMS))
    for item in detail["items"]:
        assert item["concept_id"] == concept_id_for(DSA_TRACK_ID, item["label"])
        assert item["concept_key"] == pattern_slug(item["label"])
    assert detail["concept_count"] == 0 and detail["due_today"] == []


async def test_library_and_decision_tree(api):
    lib = await ok(await api.get(f"/v1/tracks/{DSA_TRACK_ID}/library"))
    assert [p["name"] for p in lib["patterns"]] == fx.PATTERNS
    stack = lib["patterns"][3]
    assert stack["concept_key"] == "stack" and stack["problem_count"] == 3
    assert stack["library"]["when_to_reach"].startswith("Next-greater")
    assert stack["library"]["tells"] == ["“next greater”", "it's nested"]
    assert stack["library"]["animation_id"] == "stack"
    assert lib["patterns"][1]["library"]["animation_id"] is None  # no reel for it
    assert lib["patterns"][0]["concept_key"] == "arrays-and-hashing"

    tree = await ok(await api.get(f"/v1/tracks/{DSA_TRACK_ID}/decision-tree"))
    assert tree["root"] == "q_sorted"
    nodes = {n["id"]: n for n in tree["nodes"]}
    assert nodes["q_sorted"]["yes"] == "leaf_binary" and nodes["q_sorted"]["kind"] == "question"
    assert nodes["leaf_stack"]["concept_id"] == concept_id_for(DSA_TRACK_ID, "Stack")
    own = await new_track(api, "Mine")
    assert (await api.get(f"/v1/tracks/{own}/library")).status_code == 404
    assert (await api.get(f"/v1/tracks/{own}/decision-tree")).status_code == 404


async def test_concept_page_of_a_pattern_carries_the_library_and_hides_problems(api):
    concept_id = concept_id_for(DSA_TRACK_ID, "Binary Search")
    page = await ok(await api.get(f"/v1/concepts/{concept_id}"))  # before any review
    assert page["state"] == "new" and page["timeline"] == []
    assert page["library"]["pseudocode"] == ["lo, hi = 0, n-1"]
    assert page["question_versions"] == [] and page["current_question"] is None
    assert page["track_name"] == "Data structures and algorithms"


# --------------------------------------------------------------------------- #
# DSA sessions
# --------------------------------------------------------------------------- #
async def test_dsa_session_needs_an_enrolment(api):
    assert (await dsa_session(api))["items"] == []
    resp = await api.post(
        "/v1/review-sessions", json={"kind": "dsa_patterns", "track_id": DSA_TRACK_ID}
    )
    assert resp.status_code == 404
    await enrol(api)
    assert (await dsa_session(api, track_id=DSA_TRACK_ID))["items"]


async def test_dsa_session_follows_the_v1_draw_rules(api):
    await enrol(api)
    for _ in range(25):
        session = await dsa_session(api, time_budget_minutes=15)
        items = session["items"]
        assert items and session["time_budget_minutes"] == 15
        weights = [i["pattern_match"]["time_weight"] for i in items]
        assert sum(weights) == session["time_weight_total"] <= 15
        rows = [problem_of(i["question_id"]) for i in items]
        ids = {r[0] for r in rows}
        assert not {6, 7} <= ids  # never two near duplicates in one session
        assert len(ids) == len(rows)  # without replacement
        for item, row in zip(items, rows):
            assert item["question_type"] == "pattern_match"
            assert item["pattern_match"]["difficulty"] == row[1]
            assert item["pattern_match"]["time_weight"] == fx.WEIGHT[row[1]]
    # A small clock fits only Easy problems; one too small for any is empty.
    easy = await dsa_session(api, time_budget_minutes=3)
    assert [problem_of(i["question_id"])[1] for i in easy["items"]] == ["Easy"]
    assert (await dsa_session(api, time_budget_minutes=1))["items"] == []
    hard_only = await dsa_session(
        api, time_budget_minutes=7, difficulty_mix={"easy": 0, "medium": 0, "hard": 1}
    )
    assert [problem_of(i["question_id"])[1] for i in hard_only["items"]] == ["Hard"]


async def test_the_answer_is_withheld_until_stage_two(api, runtime, mcp_user):
    await enrol(api)
    session = await dsa_session(api, time_budget_minutes=60)
    body = json.dumps(session)
    for row in fx.PROBLEMS:
        assert fx.title_of(row[0]) not in body
        assert f"quokka-riddle-{row[0]}" not in body
    for pattern in fx.PATTERNS:
        assert concept_id_for(DSA_TRACK_ID, pattern) not in body
    item = session["items"][0]
    assert item["concept_id"] == DSA_TRACK_ID and item["concept_title"] == "Pattern match"
    lc, _, algorithm, _, _ = problem_of(item["question_id"])
    pm = item["pattern_match"]
    assert pm["description"] == fx.description_of(lc) == item["stem"]
    assert pm["io_input"].startswith("A list of parcel numbers")
    assert pm["io_contract"].startswith("Input: ")
    assert pm["examples"][0] == {"input": "[1, 2]", "output": "1", "note": "The first one."}

    s1 = await ok(
        await api.post(
            f"/v1/reviews/{item['question_id']}/stage1",
            json={"concept_id": item["concept_id"], "free_text": "two indices?"},
        )
    )
    assert s1["options"] == fx.PATTERNS
    assert fx.title_of(lc) not in json.dumps(s1)
    pending = await ok(await api.get(f"/v1/reviews/{s1['review_id']}"))
    assert pending["concept_id"] == DSA_TRACK_ID  # still masked between the stages
    # A pattern_match answer never waits for the sync's grader.
    assert await SyncService(runtime).get_pending_gradings(mcp_user.user_id) == []

    s2 = await ok(
        await api.post(
            f"/v1/reviews/{s1['review_id']}/stage2",
            json={"selected_index": fx.PATTERNS.index(algorithm)},
        )
    )
    assert s2["match"] == "correct" and s2["correct"] is True
    assert s2["question_title"] == fx.title_of(lc)
    assert s2["leetcode_url"] == f"https://leetcode.com/problems/quokka-riddle-{lc}/"
    assert s2["concept_id"] == concept_id_for(DSA_TRACK_ID, algorithm)
    assert s2["expected_answer"] == algorithm
    assert s2["grading"] == "graded" and s2["provisional_grade"] == 3
    assert s2["feedback"] == f"Correct: this is a {algorithm} problem."
    review = await ok(await api.get(f"/v1/reviews/{s1['review_id']}"))
    assert review["concept_id"] == s2["concept_id"] and review["final_grade"] == 3


async def test_partial_and_incorrect_scores_update_the_pattern(api, runtime, mcp_user):
    await enrol(api)
    q3 = question_id_for(DSA_TRACK_ID, 3)  # Two Pointers; Arrays & Hashing is acceptable
    item = {"question_id": q3, "concept_id": DSA_TRACK_ID}
    _, partial = await answer_with(api, item, "Arrays & Hashing")
    assert partial["match"] == "partial" and partial["correct"] is False
    assert partial["provisional_grade"] == 2 and partial["grading"] == "graded"
    assert partial["feedback"].startswith("Defensible: Arrays & Hashing")
    _, wrong = await answer_with(api, item, "Stack")
    assert wrong["match"] == "incorrect" and wrong["provisional_grade"] == 1

    two_pointers = concept_id_for(DSA_TRACK_ID, "Two Pointers")
    card = await runtime.repos.concept_states.for_concept(mcp_user.user_id, two_pointers)
    assert card is not None and card.track_id == DSA_TRACK_ID and card.reps == 2
    assert len(card.active_question_ids) == 2
    detail = await ok(await api.get(f"/v1/tracks/{DSA_TRACK_ID}"))
    states = {i["label"]: i["state"] for i in detail["items"]}
    assert states["Two Pointers"] != "new" and states["Stack"] == "new"
    assert detail["concept_count"] == 1
    page = await ok(await api.get(f"/v1/concepts/{two_pointers}"))
    assert [e["grade"] for e in page["timeline"]] == [2, 1]
    assert page["question_versions"] == []


async def test_due_patterns_come_first_and_join_the_daily_queue(api, runtime, mcp_user):
    await enrol(api)
    _, s2 = await answer_with(
        api, {"question_id": question_id_for(DSA_TRACK_ID, 8), "concept_id": DSA_TRACK_ID}, "Stack"
    )
    stack = concept_id_for(DSA_TRACK_ID, "Stack")
    card = await runtime.repos.concept_states.for_concept(mcp_user.user_id, stack)
    card.due_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await runtime.repos.concept_states.replace(card)

    # The due pattern is drawn first (its unseen problems before its recent one).
    for _ in range(10):
        first = (await dsa_session(api, time_budget_minutes=20))["items"][0]
        assert problem_of(first["question_id"])[2] == "Stack"
    # The general queue (Today) serves it too, masked; a notes session leaves it out.
    general = await ok(await api.post("/v1/review-sessions", json={}))
    (item,) = general["items"]
    assert item["question_type"] == "pattern_match" and item["concept_id"] == DSA_TRACK_ID
    notes = await ok(await api.post("/v1/review-sessions", json={"kind": "notes"}))
    assert notes["items"] == []
    due = await ok(await api.get("/v1/reviews/due"))
    assert [i["concept_id"] for i in due["items"]] == [DSA_TRACK_ID]
    listed = {t["id"]: t for t in await ok(await api.get("/v1/tracks"))}
    assert listed[DSA_TRACK_ID]["due_count"] == 1

    # Leaving the track takes it out of the queue and refuses its questions.
    await api.delete(f"/v1/tracks/{DSA_TRACK_ID}/enrol")
    assert (await ok(await api.post("/v1/review-sessions", json={})))["items"] == []
    listed = {t["id"]: t for t in await ok(await api.get("/v1/tracks"))}
    assert listed[DSA_TRACK_ID]["due_count"] == 0
    resp = await api.post(
        f"/v1/reviews/{item['question_id']}/stage1",
        json={"concept_id": DSA_TRACK_ID, "free_text": ""},
    )
    assert resp.status_code == 404


async def test_notes_tracks_are_unaffected(api, runtime, mcp_user):
    await enrol(api)
    track = await new_track(api, "Systems")
    await new_concept(track, "Quorums")
    session = await ok(await api.post("/v1/review-sessions", json={}))
    (item,) = session["items"]
    assert item["question_type"] == "free_text" and item["pattern_match"] is None
    s1 = await ok(
        await api.post(
            f"/v1/reviews/{item['question_id']}/stage1",
            json={"concept_id": item["concept_id"], "free_text": "a quorum overlaps"},
        )
    )
    s2 = await ok(
        await api.post(f"/v1/reviews/{s1['review_id']}/stage2", json={"selected_index": 0})
    )
    assert s2["match"] is None and s2["question_title"] is None and s2["grading"] == "provisional"
    assert s2["concept_id"] == item["concept_id"]
    assert len(await SyncService(runtime).get_pending_gradings(mcp_user.user_id)) == 1


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #
async def test_import_counts_and_rerun_changes_nothing(runtime, jolt_repo):
    bundle = load_bundle(jolt_repo)
    assert bundle.ok, bundle.errors
    assert bundle.patterns == fx.PATTERNS and bundle.skipped == 1
    dry = await import_dsa(runtime, bundle, dry_run=True)
    assert dry.questions.created == 10 and dry.concepts.created == 4
    assert runtime.cosmos.container("questions").all_items() == []  # dry run wrote nothing

    first = await import_dsa(runtime, bundle)
    assert (first.track, first.content) == ("created", "created")
    assert first.questions.created == 10 and first.concepts.created == 4
    snapshot = {
        name: sorted(
            json.dumps(d, sort_keys=True) for d in runtime.cosmos.container(name).all_items()
        )
        for name in ("tracks", "concepts", "questions")
    }
    second = await import_dsa(runtime, load_bundle(jolt_repo))
    assert second.changed is False
    assert second.questions.unchanged == 10 and second.concepts.unchanged == 4
    after = {
        name: sorted(
            json.dumps(d, sort_keys=True) for d in runtime.cosmos.container(name).all_items()
        )
        for name in snapshot
    }
    assert after == snapshot  # not even an etag moved

    q = await runtime.repos.questions.get_question(
        question_id_for(DSA_TRACK_ID, 3), concept_id_for(DSA_TRACK_ID, "Two Pointers")
    )
    assert q.type.value == "pattern_match" and q.title == fx.title_of(3)
    assert q.options == fx.PATTERNS and q.correct_index == 1 and q.acceptable_indexes == [0]
    assert q.pattern_match.leetcode_url.endswith("quokka-riddle-3/")
    content = await runtime.repos.track_content.get_content(DSA_TRACK_ID)
    assert len(content.problems) == 10 and content.decision_tree["root"] == "q_sorted"
    assert [p.leetcode_id for p in content.problems if p.near_duplicate_of] == [7]


async def test_reimport_updates_moves_and_prunes(runtime, tmp_path):
    repo = fx.write_repo(tmp_path / "a")
    await import_dsa(runtime, load_bundle(repo))
    rows = [list(r) for r in fx.PROBLEMS]
    rows[0][2] = "Stack"  # problem 1 retagged
    edited = [fx.problem(*r) for r in rows[:-1]]  # problem 10 dropped
    edited[1]["io"]["output"] = "A count."  # problem 2 edited
    repo_b = fx.write_repo(tmp_path / "b", problems=edited)

    report = await import_dsa(runtime, load_bundle(repo_b))
    assert report.questions.moved == 1 and report.questions.updated == 1
    assert report.questions.created == 1  # problem 1 under its new pattern
    assert report.questions.retired == 0
    content = await runtime.repos.track_content.get_content(DSA_TRACK_ID)
    assert {p.leetcode_id for p in content.problems} == set(range(1, 11))  # 10 kept
    old = await runtime.repos.questions.get_question(
        question_id_for(DSA_TRACK_ID, 1), concept_id_for(DSA_TRACK_ID, "Arrays & Hashing")
    )
    assert old.status.value == "retired"

    pruned = await import_dsa(runtime, load_bundle(repo_b), prune=True)
    assert pruned.questions.retired == 1
    content = await runtime.repos.track_content.get_content(DSA_TRACK_ID)
    assert 10 not in {p.leetcode_id for p in content.problems}
    assert (await import_dsa(runtime, load_bundle(repo_b), prune=True)).changed is False


async def test_include_new_takes_only_original_problems(runtime, tmp_path):
    ready = fx.problem(21, "Easy", "Stack", [], None)
    pending = {**fx.problem(22, "Easy", "Stack", [], None), "description": None, "io": None}
    pending["descriptionStatus"] = "pending_original_authoring"
    del pending["ioStatus"], pending["wordCount"]
    repo = fx.write_repo(tmp_path / "Jolt", new=[ready, pending])
    assert len(load_bundle(repo).problems) == 10
    bundle = load_bundle(repo, include_new=True)
    assert bundle.ok, bundle.errors
    assert len(bundle.problems) == 11 and bundle.skipped == 2
    report = await import_dsa(runtime, bundle)
    assert report.questions.created == 11


def test_validation_mirrors_check_batch(tmp_path):
    bad = [fx.problem(*r) for r in fx.PROBLEMS]
    bad[0]["description"] += " Use a hash map."  # banned term
    bad[1]["timeWeightMinutes"] = 4  # wrong weight for Easy
    bad[2]["acceptableAlgorithms"] = ["Graphs"]  # off-catalog tag
    bad[3]["io"]["examples"] = bad[3]["io"]["examples"][:1]  # too few examples
    bad[4]["description"] = "Too short."
    bad[4]["wordCount"] = 2
    bundle = load_bundle(fx.write_repo(tmp_path / "Jolt", problems=bad))
    text = "\n".join(bundle.errors)
    assert 'leaks banned term "hash"' in text
    assert "timeWeightMinutes is 4, expected 2 for Easy" in text
    assert 'off-catalog tag "Graphs"' in text
    assert "io.examples has 1, want 2–3" in text
    assert "description is 2 words, want 120–200" in text
    assert not bundle.ok


async def test_import_refuses_invalid_input_and_writes_nothing(runtime, tmp_path):
    bad = [fx.problem(*r) for r in fx.PROBLEMS]
    bad[0]["algorithm"] = "Graphs"
    with pytest.raises(ImportRefused):
        await import_dsa(runtime, load_bundle(fx.write_repo(tmp_path / "Jolt", problems=bad)))
    assert runtime.cosmos.container("questions").all_items() == []


def test_script_dry_run(tmp_path, capsys, monkeypatch):
    from scripts import import_dsa as script

    monkeypatch.setenv("STORAGE_MODE", "memory")
    from jolt.config import get_settings

    get_settings.cache_clear()
    try:
        repo = fx.write_repo(tmp_path / "Jolt")
        assert script.main(["--from", str(repo), "--dry-run", "--json"]) == 0
        out = capsys.readouterr().out
        report = json.loads(out[out.index("{") :])
        assert report["dry_run"] and report["questions"]["created"] == 10
        assert script.main(["--from", str(tmp_path / "missing")]) == 2
    finally:
        get_settings.cache_clear()


@pytest.mark.skipif(not (JOLT_REPO / "data" / "archived").is_dir(), reason="no Jolt checkout")
async def test_real_jolt_repo_imports_every_playable_problem(runtime):
    bundle = load_bundle(JOLT_REPO, include_new=True)
    assert bundle.ok, bundle.errors[:5]
    assert len(bundle.patterns) == 24
    report = await import_dsa(runtime, bundle)
    archived = [
        p
        for f in sorted((JOLT_REPO / "data" / "archived").glob("*.json"))
        for p in json.loads(f.read_text())["problems"]
        if p.get("descriptionStatus") == "original"
    ]
    assert report.questions.created == len(bundle.problems) >= len(archived)
    assert report.concepts.created == 24
    assert (await import_dsa(runtime, load_bundle(JOLT_REPO, include_new=True))).changed is False
    library = [c for c in runtime.cosmos.container("concepts").all_items() if c["library"]]
    assert len(library) == 24 and all(c["library"]["animation_id"] for c in library)


# --------------------------------------------------------------------------- #
# Units
# --------------------------------------------------------------------------- #
def _cand(qid, lc, diff, concept="c", tier=dsa.TIER_NEW, near=None, recent=False):
    return dsa.Candidate(qid, concept, lc, diff, fx.WEIGHT[diff], near, tier, recent)


def test_draw_prefers_due_patterns_and_respects_the_rules():
    pool = [
        _cand("a", 1, "Medium", "due", dsa.TIER_DUE),
        _cand("b", 2, "Medium", "new", dsa.TIER_NEW),
        _cand("c", 3, "Medium", "learning", dsa.TIER_LEARNING),
        _cand("d", 4, "Medium", "retired", dsa.TIER_RETIRED),
    ]
    for seed in range(20):
        got = dsa.draw_session(pool, 12, {"Medium": 1}, rng=random.Random(seed))
        assert [c.question_id for c in got] == ["a", "b", "c"]
    dupes = [_cand("x", 10, "Easy", near=11), _cand("y", 11, "Easy"), _cand("z", 12, "Easy")]
    for seed in range(20):
        got = {c.question_id for c in dsa.draw_session(dupes, 100, rng=random.Random(seed))}
        assert not {"x", "y"} <= got and "z" in got
    same = [_cand("p", 20, "Easy", "s"), _cand("q", 21, "Easy", "s"), _cand("r", 22, "Easy", "t")]
    got = dsa.draw_session(same, 4, rng=random.Random(1))
    assert {c.concept_id for c in got} == {"s", "t"}  # a second pattern before a repeat
    assert dsa.draw_session(same, 100, limit=1, rng=random.Random(1))[0].question_id in "pqr"


def test_match_verdict():
    assert dsa.match_verdict(2, 2, [1]) == "correct"
    assert dsa.match_verdict(1, 2, [1]) == "partial"
    assert dsa.match_verdict(0, 2, [1]) == "incorrect"


def test_ts_literal_reader():
    src = """
    // comment
    export const A: Record<string, X> = {
      plain: 'it\\'s',  "dq": "a\\u00e9b", 3: 1, list: [1, -2.5, true, null,],
      /* block */ tpl: `multi
line`,
    };
    export const B = 'x' as const;
    """
    assert read_export(src, "A") == {
        "plain": "it's",
        "dq": "aéb",
        "3": 1,
        "list": [1, -2.5, True, None],
        "tpl": "multi\nline",
    }
    with pytest.raises(LiteralError):
        read_export("export const C = { a: f(1) };", "C")
    with pytest.raises(LiteralError):
        read_export("export const D = { ...x };", "D")
    with pytest.raises(LiteralError):
        read_export("const E = 1;", "E")
    assert read_export(src, "B") == "x"
