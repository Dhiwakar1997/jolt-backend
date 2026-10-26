"""Where the two backend lines meet (BE-07/BE-09 curated DSA x BE-08/BE-10).

* `DELETE /v1/me` removes the learner's DSA enrolment, cards, answers and
  sessions, and leaves the curated track, its concepts, questions and the
  `dsa.content` document exactly as they were.
* The daily cap does not cut a DSA session (its time budget bounds it); its
  answers still count towards the cap of the next regular session.
* Progress lists the enrolled DSA track, from enrolment on.
* The maintenance sweep never touches curated content.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from jolt.curated.dsa_import import DSA_TRACK_ID, import_dsa
from jolt.curated.dsa_source import load_bundle
from jolt.data.repositories.tracks import CURATED_BUCKET
from jolt.main import create_app
from jolt.services.maintenance import MaintenanceService
from tests import dsa_fixture as fx
from tests.journeys import new_concept, new_track, ok
from tests.test_maintenance import _source


@pytest.fixture
async def imported(runtime, tmp_path):
    return await import_dsa(runtime, load_bundle(fx.write_repo(tmp_path / "Jolt")))


@pytest.fixture
async def api(settings, runtime, mcp_user, imported):
    app = create_app(settings=settings, runtime=runtime)
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {mcp_user.app_token}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        yield c


def curated_snapshot(runtime) -> dict[str, list[str]]:
    """Every curated document (tracks bucket, the track's concepts and their
    questions), serialised, so a later comparison catches any change."""
    tracks = [
        d for d in runtime.cosmos.container("tracks").all_items() if d["user_id"] == CURATED_BUCKET
    ]
    concepts = [
        d for d in runtime.cosmos.container("concepts").all_items() if d["track_id"] == DSA_TRACK_ID
    ]
    concept_ids = {c["id"] for c in concepts}
    questions = [
        d
        for d in runtime.cosmos.container("questions").all_items()
        if d["concept_id"] in concept_ids
    ]

    def dump(docs):
        return sorted(json.dumps(d, sort_keys=True, default=str) for d in docs)

    return {"tracks": dump(tracks), "concepts": dump(concepts), "questions": dump(questions)}


async def enrol(api) -> None:
    await ok(await api.post(f"/v1/tracks/{DSA_TRACK_ID}/enrol"))


async def dsa_session(api, **body) -> dict:
    return await ok(await api.post("/v1/review-sessions", json={"kind": "dsa_patterns", **body}))


async def answer(api, item: dict, session_id: str | None = None) -> dict:
    s1 = await ok(
        await api.post(
            f"/v1/reviews/{item['question_id']}/stage1",
            json={"concept_id": item["concept_id"], "free_text": "", "session_id": session_id},
        )
    )
    return await ok(
        await api.post(f"/v1/reviews/{s1['review_id']}/stage2", json={"selected_index": 0})
    )


async def test_delete_account_keeps_curated_dsa_and_removes_the_learners_part(
    api, runtime, mcp_user
):
    uid = mcp_user.user_id
    await enrol(api)
    session = await dsa_session(api, time_budget_minutes=30)
    s2 = await answer(api, session["items"][0], session["id"])
    assert s2["match"] in ("correct", "partial", "incorrect")
    pattern_id = s2["concept_id"]
    assert await runtime.repos.concept_states.for_concept(uid, pattern_id) is not None
    assert (await runtime.repos.users.get_user(uid)).enrolled_track_ids() == {DSA_TRACK_ID}

    before = curated_snapshot(runtime)
    assert before["tracks"] and before["concepts"] and before["questions"]
    content_id = f"{DSA_TRACK_ID}.content"
    assert any(f'"id": "{content_id}"' in d for d in before["tracks"])

    resp = await api.request("DELETE", "/v1/me", json={"confirm": "DELETE"})
    assert resp.status_code == 204, resp.text

    # Curated content: identical, document for document.
    assert curated_snapshot(runtime) == before
    assert await runtime.repos.track_content.get_content(DSA_TRACK_ID) is not None
    # The learner's part is gone: enrolment (user doc), cards, answers, sessions.
    assert await runtime.repos.users.get_user(uid) is None
    assert await runtime.repos.concept_states.all_for_user(uid) == []
    assert await runtime.repos.reviews.list_partition(uid) == []
    assert await runtime.repos.review_sessions.list_partition(uid) == []

    # Another learner still finds the track in the catalog and can enrol.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings=runtime.settings, runtime=runtime)),
        base_url="http://t",
    ) as other:
        signed = await ok(await other.post("/v1/auth/dev", json={"email": "next@example.com"}))
        other.headers["Authorization"] = f"Bearer {signed['app_token']}"
        catalog = await ok(await other.get("/v1/tracks/catalog"))
        assert [t["id"] for t in catalog] == [DSA_TRACK_ID]
        assert catalog[0]["enrolled"] is False
        await enrol(other)
        assert (await dsa_session(other, time_budget_minutes=30))["items"]


async def test_daily_cap_does_not_cut_a_dsa_session_but_its_answers_count(api):
    """Decision (lines merge): a DSA session is bounded by its time budget, not
    by the daily cap; every answer, DSA included, counts in `done_today`, so
    towards the cap of the next regular session."""
    await enrol(api)
    track = await new_track(api, "Systems")
    await new_concept(track, "Quorums")
    await new_concept(track, "Leases")
    await ok(await api.patch("/v1/settings", json={"daily_review_cap": 1}))

    session = await dsa_session(api, time_budget_minutes=60)
    assert len(session["items"]) > 1  # more than the cap
    assert session["capped"] is False and session["daily_review_cap"] == 1
    assert session["time_budget_minutes"] == 60

    await answer(api, session["items"][0], session["id"])

    notes = await ok(await api.post("/v1/review-sessions", json={"kind": "notes"}))
    assert notes["done_today"] == 1
    assert notes["items"] == [] and notes["capped"] is True
    # The next DSA session is still served in full.
    again = await dsa_session(api, time_budget_minutes=60)
    assert again["items"] and again["capped"] is False and again["done_today"] == 1


async def test_progress_lists_the_enrolled_dsa_track(api):
    rows = (await ok(await api.get("/v1/progress")))["tracks"]
    assert DSA_TRACK_ID not in [r["track_id"] for r in rows]

    await enrol(api)
    (row,) = [r for r in (await ok(await api.get("/v1/progress")))["tracks"]]
    assert row["track_id"] == DSA_TRACK_ID and row["kind"] == "dsa_patterns"
    assert (row["learning"], row["mastered"], row["due"]) == (0, 0, 0)

    session = await dsa_session(api, time_budget_minutes=30)
    await answer(api, session["items"][0], session["id"])
    progress = await ok(await api.get("/v1/progress"))
    (row,) = [r for r in progress["tracks"] if r["track_id"] == DSA_TRACK_ID]
    assert row["learning"] + row["mastered"] == 1 and row["retention"] > 0
    assert progress["reviews_total"] == 1

    # Leaving keeps the history visible (cards are kept, the track is paused).
    assert (await api.delete(f"/v1/tracks/{DSA_TRACK_ID}/enrol")).status_code == 204
    rows = (await ok(await api.get("/v1/progress")))["tracks"]
    assert DSA_TRACK_ID in [r["track_id"] for r in rows]


async def test_maintenance_sweep_never_touches_curated_content(api, runtime, mcp_user):
    uid = mcp_user.user_id
    await enrol(api)
    session = await dsa_session(api, time_budget_minutes=30)
    await answer(api, session["items"][0], session["id"])
    user = await runtime.repos.users.get_user(uid)
    user.settings.data_retention_days = 1
    await runtime.repos.users.replace(user)
    old = await _source(runtime, uid, "old", created=datetime.now(timezone.utc))
    before = curated_snapshot(runtime)
    cards = await runtime.repos.concept_states.all_for_user(uid)
    assert cards

    # Far enough ahead that everything, curated documents included, is "old".
    later = datetime.now(timezone.utc) + timedelta(days=400)
    report = await MaintenanceService(runtime).sweep(now=later)

    assert report.expired_sources == 1  # the sweep did run, on the user's upload
    assert await runtime.repos.sources.get_source(old, uid) is None
    assert curated_snapshot(runtime) == before
    assert await runtime.repos.track_content.get_content(DSA_TRACK_ID) is not None
    assert len(await runtime.repos.concept_states.all_for_user(uid)) == len(cards)
    assert (await runtime.repos.users.get_user(uid)).enrolled_track_ids() == {DSA_TRACK_ID}
