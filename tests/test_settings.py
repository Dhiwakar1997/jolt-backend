"""BE-08: the Settings body, the model status block, and settings taking effect on
the next review session (desired retention → FSRS intervals, daily cap →
session size, time zone → "today" and upload dates)."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from jolt.domain.models import User, UserSettings
from jolt.domain.sync import upload_day
from jolt.main import create_app
from jolt.mcp import server as mcp_server
from jolt.mcp.schemas import validate_output
from jolt.services.reviews import ReviewService

TOOLS = dict(mcp_server._TOOLS)
LONG = (
    "Consistent hashing puts keys and nodes on one ring; each key belongs to the next "
    "node clockwise, so adding a node only moves the keys between it and its predecessor."
)


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


async def new_concepts(api, n: int, track: str = "Systems") -> list[str]:
    track_id = (await ok(await api.post("/v1/tracks", json={"name": track})))["id"]
    ids = []
    for i in range(n):
        logged = await call(
            "jolt_log_session",
            source_ids=[],
            concepts=[{"text": f"Idea {i}", "title": f"Idea {i}", "track_id": track_id}],
        )
        (cid,) = logged["concept_ids"]
        await call(
            "jolt_create_questions",
            concept_id=cid,
            questions=[
                {
                    "stem": f"Idea {i}?",
                    "options": ["right", "w1", "w2", "w3"],
                    "correct_index": 0,
                    "expected_answer": "right",
                }
            ],
        )
        ids.append(cid)
    return ids


async def answer(api, item: dict, *, text="short", latency=10_000) -> dict:
    s1 = await ok(
        await api.post(
            f"/v1/reviews/{item['question_id']}/stage1",
            json={"concept_id": item["concept_id"], "free_text": text},
        )
    )
    return await ok(
        await api.post(
            f"/v1/reviews/{s1['review_id']}/stage2",
            json={"selected_index": 0, "latency_ms": latency},
        )
    )


# --------------------------------------------------------------------------- #
async def test_settings_defaults_and_model_status(api, mcp_user):
    body = await ok(await api.get("/v1/settings"))
    status = body.pop("model_status")
    assert body == {
        "theme": "system",
        "desired_retention": 0.9,  # FSRS_DESIRED_RETENTION until the user picks one
        "retire_mastered": True,
        "allow_question_rewrite": False,
        "daily_review_cap": None,
        "data_retention_days": None,
        "time_zone": "UTC",
    }
    assert status["mcp_server_url"].endswith("/mcp/sse")
    assert [t["label"] for t in status["tokens"]] == ["Test agent"]
    assert set(status["tokens"][0]) == {"id", "label", "last4", "created_at", "last_used_at"}
    assert (status["last_sync"], status["health"]) == (None, "never")
    assert "T00:15:00" in status["next_expected_at"]  # 00:15 in UTC, the default zone

    # A sync run shows up as last_sync with health ok.
    plan = await call("jolt_sync")
    assert plan["done"]
    status = (await ok(await api.get("/v1/settings")))["model_status"]
    assert status["health"] == "ok"
    assert status["last_sync"]["client_label"] == "Test agent"
    assert status["last_sync"]["status"] == "finished"


async def test_patch_round_trip_and_validation(api):
    patch = {
        "theme": "dark",
        "desired_retention": 0.85,
        "daily_review_cap": 40,
        "data_retention_days": 365,
        "time_zone": "Europe/London",
        "allow_question_rewrite": True,
    }
    body = await ok(await api.patch("/v1/settings", json=patch))
    body.pop("model_status")
    assert body == {**patch, "retire_mastered": True}
    got = await ok(await api.get("/v1/settings"))
    got.pop("model_status")
    assert got == body

    # null clears the nullable ones and resets desired retention to the default.
    body = await ok(
        await api.patch(
            "/v1/settings",
            json={"daily_review_cap": None, "data_retention_days": None, "desired_retention": None},
        )
    )
    assert (body["daily_review_cap"], body["data_retention_days"]) == (None, None)
    assert body["desired_retention"] == 0.9
    assert body["theme"] == "dark"  # untouched

    for bad in (
        {"desired_retention": 0.79},
        {"desired_retention": 0.98},
        {"theme": "sepia"},
        {"theme": None},
        {"retire_mastered": None},
        {"time_zone": "Mars/Olympus"},
        {"time_zone": None},
        {"daily_review_cap": 0},
        {"daily_review_cap": 501},
        {"data_retention_days": 0},
    ):
        resp = await api.patch("/v1/settings", json=bad)
        assert resp.status_code == 422, (bad, resp.text)


async def test_put_retention_is_data_retention_and_legacy_docs_migrate(api, runtime, mcp_user):
    assert (await ok(await api.put("/v1/settings/retention", json={"retention_days": 90}))) == {
        "retention_days": 90
    }
    assert (await ok(await api.get("/v1/settings")))["data_retention_days"] == 90
    # FSRS retention is a different setting and did not move.
    assert (await ok(await api.get("/v1/settings")))["desired_retention"] == 0.9

    # A user document written before BE-08 kept it at the top level.
    legacy = User.model_validate({"id": "u1", "retention_days": 30})
    assert legacy.settings.data_retention_days == 30 and legacy.retention_days is None
    both = User.model_validate(
        {"id": "u1", "retention_days": 30, "settings": {"data_retention_days": 7}}
    )
    assert both.settings.data_retention_days == 7


async def test_api_key_list_has_added_at_and_me_has_api_key(api, monkeypatch):
    from jolt.credentials import providers
    from jolt.credentials.providers import ValidationResult

    async def fake_validate(self, key):  # no network in tests
        return ValidationResult(ok=True, last4=key[-4:])

    monkeypatch.setattr(providers.OpenAIAdapter, "validate", fake_validate)
    assert (await ok(await api.get("/v1/me")))["has_api_key"] is False
    added = await ok(
        await api.post("/v1/settings/api-key", json={"provider": "openai", "key": "sk-abcd"})
    )
    listed = await ok(await api.get("/v1/settings/api-key"))
    assert listed == [
        {"provider": "openai", "last4": "abcd", "status": "stored", "added_at": added["added_at"]}
    ]
    assert (await ok(await api.get("/v1/me")))["has_api_key"] is True


# --------------------------------------------------------------------------- #
# Settings take effect on the next review session
# --------------------------------------------------------------------------- #
async def test_desired_retention_shapes_the_next_answers_interval(api):
    """An Easy first answer puts the card in review with S = 15.69 days; the
    interval is S / (19/81) * (r ** -2 - 1): about 4 days at 0.97, 16 at 0.90 and
    38 at 0.80, give or take FSRS's interval fuzz."""
    first, second = await new_concepts(api, 2)

    await ok(await api.patch("/v1/settings", json={"desired_retention": 0.97}))
    session = await ok(await api.post("/v1/review-sessions", json={"concept_id": first}))
    strict = await answer(api, session["items"][0], text=LONG, latency=3_000)
    assert strict["provisional_grade"] == 4
    assert 2 <= strict["interval_days"] <= 7

    await ok(await api.patch("/v1/settings", json={"desired_retention": 0.80}))
    session = await ok(await api.post("/v1/review-sessions", json={"concept_id": second}))
    relaxed = await answer(api, session["items"][0], text=LONG, latency=3_000)
    assert relaxed["provisional_grade"] == 4
    assert 30 <= relaxed["interval_days"] <= 46


async def test_daily_cap_bounds_the_next_session(api):
    concepts = await new_concepts(api, 3)

    await ok(await api.patch("/v1/settings", json={"daily_review_cap": 2}))
    session = await ok(await api.post("/v1/review-sessions", json={}))
    assert len(session["items"]) == 2
    assert (session["daily_review_cap"], session["capped"]) == (2, True)
    for item in session["items"]:
        await answer(api, item)

    # Two answered today: nothing left of the cap, though one is still due.
    session = await ok(await api.post("/v1/review-sessions", json={}))
    assert session["items"] == [] and session["done_today"] == 2 and session["capped"] is True
    # "Practise this concept" is an explicit ask: not capped.
    practise = await ok(await api.post("/v1/review-sessions", json={"concept_id": concepts[0]}))
    assert len(practise["items"]) == 1

    # Raising the cap releases the rest; the cap no longer cuts anything.
    await ok(await api.patch("/v1/settings", json={"daily_review_cap": 10}))
    session = await ok(await api.post("/v1/review-sessions", json={}))
    assert len(session["items"]) == 1 and session["capped"] is False
    # A request limit under the cap is not "capped".
    await ok(await api.patch("/v1/settings", json={"daily_review_cap": None}))
    session = await ok(await api.post("/v1/review-sessions", json={"limit": 1}))
    assert (session["daily_review_cap"], session["capped"]) == (None, False)


def test_today_starts_at_the_learners_midnight():
    now = datetime(2026, 3, 18, 1, 30, tzinfo=timezone.utc)
    utc = ReviewService.today_start(UserSettings(), now)
    assert utc == datetime(2026, 3, 18, tzinfo=timezone.utc)
    # 01:30 UTC is 21:30 the day before in New York (EDT, UTC-4).
    ny = ReviewService.today_start(UserSettings(time_zone="America/New_York"), now)
    assert ny == datetime(2026, 3, 17, 4, 0, tzinfo=timezone.utc)
    # An unknown stored zone falls back to UTC rather than failing.
    assert ReviewService.today_start(UserSettings(time_zone="Nowhere/Land"), now) == utc


def test_upload_day_uses_the_learners_zone():
    from zoneinfo import ZoneInfo

    late = datetime(2026, 3, 18, 2, 0, tzinfo=timezone.utc)  # 22:00 on 03-17 in New York
    assert upload_day(late) == "2026-03-18"
    assert upload_day(late, ZoneInfo("America/New_York")) == "2026-03-17"


async def test_sessions_and_sync_status_use_the_users_zone(api, runtime, mcp_user):
    await ok(await api.patch("/v1/settings", json={"time_zone": "Asia/Kolkata"}))
    status = await ok(await api.get("/v1/sync/status"))
    # 00:15 in Kolkata (UTC+5:30) is 18:45 UTC the day before.
    assert "T18:45:00" in status["next_expected_at"]
    explicit = await ok(await api.get("/v1/sync/status", params={"tz": "UTC"}))
    assert "T00:15:00" in explicit["next_expected_at"]

    # D6: an upload confirmed at 20:00 UTC is the next day in Kolkata.
    from jolt.domain.models import Source

    uid = mcp_user.user_id
    source = await runtime.repos.sources.create(
        Source(
            user_id=uid,
            filename="n.jpg",
            content_type="image/jpeg",
            sha256="0" * 64,
            blob_path=f"{uid}/s/0-n.jpg",
            confirmed_at=datetime(2026, 3, 17, 20, 0, tzinfo=timezone.utc),
        )
    )
    track_id = (await ok(await api.post("/v1/tracks", json={"name": "T"})))["id"]
    plan = await call("jolt_sync")
    assert plan["upload_dates"] == ["2026-03-18"]
    logged = await call(
        "jolt_log_session",
        source_ids=[source.id],
        concepts=[{"text": "x", "track_id": track_id, "source_id": source.id}],
    )
    (session,) = [
        s
        for s in runtime.cosmos.container("sessions").all_items()
        if s["id"] == logged["session_id"]
    ]
    assert session["upload_date"] == "2026-03-18"
