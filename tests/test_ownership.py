"""BE-07 ownership checks (decision A16, docs/GAPS.md issue 1).

Concepts and questions are partitioned by track and concept, not by user. Every
concept-, question-, track- and source-keyed MCP tool and REST route must refuse
another user's ids exactly as it refuses unknown ones, and must not change the
owner's data. Curated tracks are readable by everyone but only show each
learner their own concepts, and nobody can write to them.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from jolt.domain.models import (
    Agenda,
    AgendaStatus,
    Concept,
    ConceptState,
    Source,
    SyllabusItem,
    Track,
    TrackOrigin,
)
from jolt.main import create_app
from jolt.mcp import context as mcp_context
from jolt.services.accounts import AccountService
from tests.journeys import TOOLS, answer, call, new_track, ok, read_source, upload


@pytest.fixture
async def api(settings, runtime, mcp_user):
    app = create_app(settings=settings, runtime=runtime)
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {mcp_user.app_token}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        yield c


@pytest.fixture
async def world(api, runtime, mcp_user):
    """The owner's track, source + read, concept + question + answered review,
    a correction note; a curated track holding one owner concept; and an
    intruder with an app token, an MCP token and a track of their own."""
    tid = await new_track(api, "Owner track", syllabus=["quorums"])
    sid = await upload(api, runtime, tid)
    await read_source(sid, concepts=[{"text": "Quorums need overlap.", "concept_key": "quorums"}])
    detail = await ok(await api.get(f"/v1/tracks/{tid}"))
    (cid,) = [i["concept_id"] for i in detail["items"] if i["concept_id"]]
    qid = (await call("jolt_create_questions", concept_id=cid, questions=[_q("Why overlap?")]))[
        "question_ids"
    ][0]
    session = await ok(await api.post("/v1/review-sessions", json={"concept_id": cid}))
    s1, _ = await answer(api, session["items"][0])
    await ok(await api.post(f"/v1/concepts/{cid}/correction", json={"note": "typo"}))

    curated = await runtime.repos.tracks.create(
        Track(
            name="Curated",
            origin=TrackOrigin.JOLT,
            # Deliberately unlocked: only ownership must stop writes to it.
            agenda=Agenda(
                status=AgendaStatus.REFINED, syllabus=[SyllabusItem(concept_key="a", label="A")]
            ),
        )
    )
    curated_concept = await runtime.repos.concepts.create(
        Concept(track_id=curated.id, text="Owner's note on a curated track", syllabus_ref="a")
    )
    await runtime.repos.concept_states.create(
        ConceptState(
            user_id=mcp_user.user_id,
            concept_id=curated_concept.id,
            track_id=curated.id,
            active_question_ids=[],
        )
    )

    service = AccountService(runtime)
    eve = await service.sign_in_dev("eve@example.com")
    eve_mcp = await service.create_mcp_token(eve.user.id, "Eve's agent")
    eve_headers = {"Authorization": f"Bearer {eve.app_token}"}
    eve_tid = (await ok(await api.post("/v1/tracks", json={"name": "Eve"}, headers=eve_headers)))[
        "id"
    ]
    return {
        "tid": tid,
        "sid": sid,
        "cid": cid,
        "qid": qid,
        "rid": s1["review_id"],
        "curated": curated.id,
        "curated_concept": curated_concept.id,
        "eve_headers": eve_headers,
        "eve_mcp": eve_mcp.token,
        "eve_tid": eve_tid,
        "owner_mcp": mcp_user.token,
        "eve_id": eve.user.id,
    }


def _q(stem: str) -> dict:
    return {"stem": stem, "options": ["a", "b"], "correct_index": 0, "expected_answer": stem}


def as_eve(world) -> None:
    mcp_context.set_current_token(world["eve_mcp"])


def as_owner(world) -> None:
    mcp_context.set_current_token(world["owner_mcp"])


async def raises(name: str, **kwargs) -> str:
    with pytest.raises(Exception) as info:
        await TOOLS[name](**kwargs)
    return str(info.value)


async def _snapshot(runtime) -> dict:
    """Every stored document except tokens/users (for "nothing changed")."""
    out = {}
    for name in ("tracks", "concepts", "concept_states", "questions", "reviews", "extractions"):
        docs = runtime.cosmos.container(name).all_items()
        out[name] = sorted((d["id"], d["_etag"]) for d in docs)
    return out


# --------------------------------------------------------------------------- #
# MCP tools
# --------------------------------------------------------------------------- #
async def test_mcp_tools_refuse_another_users_ids(world, runtime):
    w = world
    before = await _snapshot(runtime)
    as_eve(w)

    assert "track not found" in await raises("jolt_get_recent_concepts", track_id=w["tid"])
    assert "track not found" in await raises("jolt_get_coverage", track_id=w["tid"])
    assert await call("jolt_get_agenda", track_id=w["tid"]) == {"error": "track not found"}
    assert (
        await call(
            "jolt_set_agenda",
            track_id=w["tid"],
            syllabus=[{"concept_key": "x", "label": "X"}],
        )
    ) == {"error": "track not found"}
    assert "track not found" in await raises(
        "jolt_diff_extractions", track_id=w["tid"], proposed_concepts=[{"text": "x"}]
    )
    assert "track not found" in await raises(
        "jolt_log_session", source_ids=[], concepts=[{"text": "x", "track_id": w["tid"]}]
    )
    # Eve's own track, but the owner's source: refused, nothing written.
    assert "source not found" in await raises(
        "jolt_log_session",
        source_ids=[],
        concepts=[{"text": "x", "track_id": w["eve_tid"], "source_id": w["sid"]}],
    )
    assert "source not found" in await raises(
        "jolt_log_session", source_ids=[w["sid"]], concepts=[]
    )
    assert "concept not found" in await raises(
        "jolt_create_questions", concept_id=w["cid"], questions=[_q("Mine now?")]
    )
    assert await call(
        "jolt_correct_concept", track_id=w["tid"], concept_id=w["cid"], text="pwned"
    ) == {"error": "concept not found"}
    assert "source not found" in await raises(
        "jolt_store_extraction", source_id=w["sid"], markdown="pwned"
    )
    assert "source not found" in await raises(
        "jolt_store_extraction", source_id=w["sid"], markdown="pwned", concepts=[{"text": "x"}]
    )
    assert "source not found" in await raises("jolt_continue_reconciliation", source_id=w["sid"])
    assert "source not found" in await raises("jolt_get_source_content", source_id=w["sid"])
    graded = await call(
        "jolt_submit_gradings", gradings=[{"review_id": w["rid"], "suggested_fsrs_grade": 4}]
    )
    assert graded["applied"] == 0
    assert (await call("jolt_get_pending_gradings"))["gradings"] == []
    assert (await call("jolt_get_pending_corrections"))["corrections"] == []
    assert (await call("jolt_sync"))["pending_corrections"] == 0
    assert (await call("jolt_list_sources"))["sources"] == []
    assert {t["id"] for t in (await call("jolt_get_tracks"))["tracks"]} == {
        w["eve_tid"],
        w["curated"],
    }
    # Eve's own source, but concepts aimed at the owner's track: refused before
    # anything is stored.
    eve_source = await runtime.repos.sources.create(
        Source(
            user_id=w["eve_id"],
            filename="e.png",
            content_type="image/png",
            sha256="0" * 64,
            blob_path="x",
            confirmed_at=datetime.now(timezone.utc),
        )
    )
    assert "track not found" in await raises(
        "jolt_store_extraction",
        source_id=eve_source.id,
        markdown="x",
        concepts=[{"text": "x", "track_id": w["tid"]}],
    )
    assert await runtime.repos.extractions.history_for_source(eve_source.id) == []

    assert await _snapshot(runtime) == before  # the owner's data is untouched
    as_owner(w)


async def test_mcp_curated_tracks_are_read_only_and_private_per_learner(world, runtime):
    w = world
    as_eve(w)
    assert (await call("jolt_get_recent_concepts", track_id=w["curated"]))["concepts"] == []
    cov = await call("jolt_get_coverage", track_id=w["curated"])
    assert cov["total_concepts"] == 0
    diff = await call(
        "jolt_diff_extractions", track_id=w["curated"], proposed_concepts=[{"text": "x"}]
    )
    assert [d["change"] for d in diff["deltas"]] == ["new"]  # the owner's concept is invisible
    assert (await call("jolt_get_agenda", track_id=w["curated"]))["status"] == "refined"
    assert (
        await call(
            "jolt_set_agenda", track_id=w["curated"], syllabus=[{"concept_key": "x", "label": "X"}]
        )
    ) == {"error": "track not found"}
    assert "track not found" in await raises(
        "jolt_log_session", source_ids=[], concepts=[{"text": "x", "track_id": w["curated"]}]
    )
    assert await call(
        "jolt_correct_concept",
        track_id=w["curated"],
        concept_id=w["curated_concept"],
        text="pwned",
    ) == {"error": "concept not found"}

    as_owner(w)
    recent = await call("jolt_get_recent_concepts", track_id=w["curated"])
    assert [c["id"] for c in recent["concepts"]] == [w["curated_concept"]]
    assert (await call("jolt_get_coverage", track_id=w["curated"]))["total_concepts"] == 1
    # The owner cannot write to a curated agenda either.
    assert (
        await call(
            "jolt_set_agenda", track_id=w["curated"], syllabus=[{"concept_key": "x", "label": "X"}]
        )
    ) == {"error": "track not found"}


async def test_correct_concept_needs_the_concepts_own_track(world):
    w = world
    as_owner(w)
    assert await call(
        "jolt_correct_concept", track_id=w["curated"], concept_id=w["cid"], text="x"
    ) == {"error": "concept not found"}


# --------------------------------------------------------------------------- #
# REST routes
# --------------------------------------------------------------------------- #
async def test_rest_routes_refuse_another_users_ids(api, world, runtime):
    w = world
    eve = w["eve_headers"]
    before = await _snapshot(runtime)
    cid, qid, tid, sid, rid = w["cid"], w["qid"], w["tid"], w["sid"], w["rid"]

    not_found = [
        ("GET", f"/v1/tracks/{tid}", None),
        ("PATCH", f"/v1/tracks/{tid}", {"name": "pwned"}),
        ("PATCH", f"/v1/tracks/{tid}", {"archived": True}),
        ("POST", f"/v1/tracks/{tid}/agenda/lock", None),
        ("POST", f"/v1/tracks/{tid}/agenda/unlock", None),
        ("GET", f"/v1/tracks/{tid}/coverage", None),
        ("GET", f"/v1/concepts/{cid}", None),
        ("POST", f"/v1/concepts/{cid}/correction", {"note": "pwned"}),
        ("POST", f"/v1/concepts/{cid}/unretire", None),
        ("GET", f"/v1/concepts/{cid}/questions", None),
        ("GET", f"/v1/progress/concepts/{cid}", None),
        ("POST", f"/v1/reviews/{qid}/stage1", {"concept_id": cid, "free_text": "x"}),
        ("POST", "/v1/review-sessions", {"track_id": tid}),
        ("POST", "/v1/review-sessions", {"concept_id": cid}),
        ("GET", f"/v1/reviews/{rid}", None),
        ("GET", f"/v1/sources/{sid}", None),
        ("GET", f"/v1/sources/{sid}/extractions", None),
        ("POST", f"/v1/sources/{sid}/reread", {}),
        ("PATCH", f"/v1/tracks/{w['curated']}", {"name": "pwned"}),
        ("POST", f"/v1/tracks/{w['curated']}/agenda/lock", None),
        (
            "POST",
            "/v1/uploads/request",
            {
                "track_id": tid,
                "filename": "x.png",
                "content_type": "image/png",
                "sha256": "0" * 64,
            },
        ),
    ]
    for method, path, body in not_found:
        resp = await api.request(method, path, json=body, headers=eve)
        assert resp.status_code == 404, (method, path, resp.status_code, resp.text)

    stage2 = await api.post(f"/v1/reviews/{rid}/stage2", json={"selected_index": 0}, headers=eve)
    assert stage2.status_code == 400 and stage2.json()["detail"] == "review not found"
    confirm = await api.post(f"/v1/uploads/{sid}/confirm", json={}, headers=eve)
    assert confirm.status_code == 400
    assert await ok(await api.get("/v1/tracks", headers=eve)) != []
    assert {t["id"] for t in await ok(await api.get("/v1/tracks", headers=eve))} == {
        w["eve_tid"],
        w["curated"],
    }
    assert await ok(await api.get("/v1/sources", headers=eve)) == []

    # A curated track is readable, but shows only the caller's own concepts.
    curated = await ok(await api.get(f"/v1/tracks/{w['curated']}", headers=eve))
    assert [i["concept_id"] for i in curated["items"]] == [None]
    assert curated["concept_count"] == 0 and curated["enrolled"] is False
    mine = await ok(await api.get(f"/v1/tracks/{w['curated']}"))
    assert [i["concept_id"] for i in mine["items"]] == [w["curated_concept"]]
    # BE-09: `enrolled` is a real enrolment now, not "has concepts on it".
    assert mine["enrolled"] is False

    assert await _snapshot(runtime) == before


async def test_be08_be09_routes_refuse_another_users_ids_and_show_nothing_of_theirs(
    api, world, runtime
):
    """BE-10 security review: the routes added after BE-07 (library, decision
    tree, enrol, review-session end, progress, storage, sync, settings, export)
    refuse the owner's ids for the intruder and show none of the owner's data."""
    w = world
    eve = w["eve_headers"]
    owner_session = await ok(await api.post("/v1/review-sessions", json={"concept_id": w["cid"]}))
    before = await _snapshot(runtime)

    for method, path in [
        ("GET", f"/v1/tracks/{w['tid']}/library"),
        ("GET", f"/v1/tracks/{w['tid']}/decision-tree"),
        ("POST", f"/v1/tracks/{w['tid']}/enrol"),
        ("DELETE", f"/v1/tracks/{w['tid']}/enrol"),
        ("POST", f"/v1/review-sessions/{owner_session['id']}/end"),
        ("DELETE", "/v1/mcp-tokens/" + (await ok(await api.get("/v1/mcp-tokens")))[0]["id"]),
    ]:
        resp = await api.request(method, path, headers=eve)
        assert resp.status_code == 404, (method, path, resp.status_code, resp.text)

    # Aggregates: nothing of the owner's leaks into the intruder's views.
    owner_ids = {w["tid"], w["sid"], w["cid"], w["qid"], w["rid"], owner_session["id"]}
    for path in (
        "/v1/progress",
        "/v1/storage",
        "/v1/sync/runs",
        "/v1/sync/status",
        "/v1/reviews/due",
        "/v1/settings",
        "/v1/mcp-tokens",
        "/v1/sources",
        "/v1/tracks/catalog",
        "/v1/me",
    ):
        text = (await api.get(path, headers=eve)).text
        assert not any(i in text for i in owner_ids), path
    tokens = await ok(await api.get("/v1/mcp-tokens", headers=eve))
    assert [t["label"] for t in tokens] == ["Eve's agent"]

    export = await ok(await api.post("/v1/export", headers=eve))
    path = export["download_url"].split("/sources/", 1)[1].split("?")[0]
    bundle = runtime.blob.get(path).data.decode()
    assert path.startswith(f"exports/{w['eve_id']}/")
    assert not any(i in bundle for i in owner_ids)

    assert await _snapshot(runtime) == before
