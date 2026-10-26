"""Nightly sync (BE-04): the plan, sync runs, session boundaries (D6), status,
runs and prompt routes, and how /mcp takes the token.

MCP tools are called directly (as in test_mcp_flow) with the token the
`mcp_user` fixture put in the MCP context; REST goes through the same runtime.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from jolt.domain.models import SyncRunStatus
from jolt.domain.sync import next_expected, parse_schedule_time, sync_health, upload_day
from jolt.main import create_app
from jolt.mcp import server as mcp_server
from jolt.mcp.context import set_current_token
from jolt.mcp.schemas import validate_output
from jolt.services.accounts import AccountService
from jolt.services.sync_prompt import PROMPT_PATH, load_prompt, parse_prompt

TOOLS = dict(mcp_server._TOOLS)
UTC = timezone.utc


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


async def upload(api, runtime, user_id: str, name: str, at: datetime | None = None) -> str:
    """Upload one confirmed source; optionally backdate it to `at`."""
    data = name.encode()
    sha = hashlib.sha256(data).hexdigest()
    ticket = (
        await api.post(
            "/v1/uploads/request",
            json={"filename": name, "content_type": "image/png", "sha256": sha},
        )
    ).json()
    source = await runtime.repos.sources.get_source(ticket["source_id"], user_id)
    runtime.blob.put(source.blob_path, data, "image/png")
    assert (await api.post(f"/v1/uploads/{source.id}/confirm", json={})).is_success
    if at is not None:
        source = await runtime.repos.sources.get_source(source.id, user_id)
        source.created_at = source.confirmed_at = at
        await runtime.repos.sources.replace(source)
    return source.id


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


QUESTION = {
    "stem": "Why?",
    "options": ["Right", "Wrong 1", "Wrong 2", "Wrong 3"],
    "correct_index": 0,
    "expected_answer": "Because.",
}


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #
async def test_plan_lists_counts_tracks_steps_and_limits(api, runtime, mcp_user):
    track = (await api.post("/v1/tracks", json={"name": "Networks"})).json()
    await upload(api, runtime, mcp_user.user_id, "a.png")
    await upload(api, runtime, mcp_user.user_id, "b.png")

    plan = await call("jolt_sync")
    assert plan["done"] is False
    assert plan["unprocessed_sources"] == 2 and plan["pending_gradings"] == 0
    assert plan["rereads"] == 0 and plan["reread_sources"] == []
    # Sources carry no track yet (BE-05), so every own track is listed to pick from.
    assert plan["unassigned_sources"] == 2
    assert [(t["track_id"], t["agenda_status"]) for t in plan["tracks"]] == [(track["id"], "none")]
    assert "concept_key" in plan["tracks"][0]["guidance"]
    assert plan["upload_dates"] == [upload_day(datetime.now(UTC))]
    assert [s["tool"] for s in plan["steps"]] == [
        "jolt_get_unprocessed_sources",
        "jolt_get_source_content",
        "jolt_store_extraction",
        "jolt_log_session",
        "jolt_create_questions",
        "jolt_sync",
    ]
    assert [s["step"] for s in plan["steps"]] == [1, 2, 3, 4, 5, 6]
    assert plan["steps"][0]["limit"] == 2  # min(sources_per_claim, waiting)
    assert plan["limits"]["options_per_question"] == 4
    assert plan["limits"]["lease_seconds"] == runtime.settings.lease_ttl_seconds
    assert plan["run_id"]


async def test_plan_counts_gradings_by_track_and_adds_grading_steps(api, runtime, mcp_user):
    track = (await api.post("/v1/tracks", json={"name": "DS"})).json()
    logged = await call(
        "jolt_log_session",
        source_ids=[],
        concepts=[{"text": "Idea", "track_id": track["id"], "concept_key": "idea"}],
    )
    (cid,) = logged["concept_ids"]
    (qid,) = (await call("jolt_create_questions", concept_id=cid, questions=[QUESTION]))[
        "question_ids"
    ]
    s1 = (
        await api.post(f"/v1/reviews/{qid}/stage1", json={"concept_id": cid, "free_text": "x"})
    ).json()
    await api.post(f"/v1/reviews/{s1['review_id']}/stage2", json={"selected_index": 0})

    plan = await call("jolt_sync")
    assert plan["pending_gradings"] == 1 and plan["unprocessed_sources"] == 0
    assert plan["tracks"][0]["pending_gradings"] == 1
    assert plan["tracks"][0]["agenda_status"] == "draft"  # auto-add made it a draft
    assert [s["tool"] for s in plan["steps"]] == [
        "jolt_get_pending_gradings",
        "jolt_submit_gradings",
        "jolt_sync",
    ]


async def test_plan_lists_read_again_requests_with_the_preferred_model(api, runtime, mcp_user):
    """BE-05 read again folds into the plan: the source is source work again
    (counted in unprocessed_sources) and listed in reread_sources."""
    track = (await api.post("/v1/tracks", json={"name": "Systems"})).json()
    sid = await upload(api, runtime, mcp_user.user_id, "notes.png")
    source = await runtime.repos.sources.get_source(sid, mcp_user.user_id)
    source.track_id = track["id"]
    await runtime.repos.sources.replace(source)
    await call("jolt_get_unprocessed_sources")
    await call("jolt_store_extraction", source_id=sid, markdown="first", confidence=0.5)
    assert (await call("jolt_sync"))["done"] is True

    rr = await api.post(f"/v1/sources/{sid}/reread", json={"preferred_model": "codex"})
    assert rr.status_code == 200, rr.text
    plan = await call("jolt_sync")
    assert plan["done"] is False
    assert (plan["unprocessed_sources"], plan["rereads"]) == (1, 1)
    (reread,) = plan["reread_sources"]
    assert {k: reread[k] for k in ("source_id", "title", "track_id", "preferred_model")} == {
        "source_id": sid,
        "title": "notes.png",
        "track_id": track["id"],
        "preferred_model": "codex",
    }
    assert _dt(reread["requested_at"]) <= datetime.now(UTC)
    assert [(t["track_id"], t["rereads"]) for t in plan["tracks"]] == [(track["id"], 1)]
    assert [s["tool"] for s in plan["steps"]] == [
        "jolt_get_unprocessed_sources",
        "jolt_get_source_content",
        "jolt_diff_extractions",
        "jolt_store_extraction",
        "jolt_log_session",
        "jolt_create_questions",
        "jolt_sync",
    ]
    assert "1 to read again" in plan["next"]

    (claimed,) = (await call("jolt_get_unprocessed_sources"))["sources"]
    assert claimed["supersede"] is True and claimed["preferred_model"] == "codex"
    await call(
        "jolt_store_extraction", source_id=sid, markdown="second", confidence=0.9, supersede=True
    )
    plan = await call("jolt_sync")
    assert (plan["done"], plan["rereads"], plan["reread_sources"]) == (True, 0, [])
    source = await runtime.repos.sources.get_source(sid, mcp_user.user_id)
    assert source.reread_requested_at is None and source.read_count == 2


async def test_claimed_sources_keep_the_run_open_but_others_leases_do_not(api, runtime, mcp_user):
    sid = await upload(api, runtime, mcp_user.user_id, "a.png")
    plan = await call("jolt_sync")
    await call("jolt_get_unprocessed_sources")
    plan = await call("jolt_sync")
    assert plan["done"] is False and plan["unprocessed_sources"] == 0
    assert [s["source_id"] for s in plan["claimed_sources"]] == [sid]
    assert [s["tool"] for s in plan["steps"]][0] == "jolt_get_source_content"

    # Another client (a second MCP token) sees the lease as someone else's.
    other = await AccountService(runtime).create_mcp_token(mcp_user.user_id, "Codex")
    set_current_token(other.token)
    plan = await call("jolt_sync")
    assert plan["done"] is True and plan["in_flight_elsewhere"] == 1
    set_current_token(mcp_user.token)


# --------------------------------------------------------------------------- #
# Session boundary (decision D6)
# --------------------------------------------------------------------------- #
async def test_multi_day_backlog_logs_one_session_per_upload_date(api, runtime, mcp_user):
    track = (await api.post("/v1/tracks", json={"name": "History"})).json()
    tid = track["id"]
    d1 = datetime(2026, 10, 20, 21, 0, tzinfo=UTC)
    d2 = datetime(2026, 10, 22, 8, 30, tzinfo=UTC)
    a = await upload(api, runtime, mcp_user.user_id, "mon-1.png", d1)
    b = await upload(api, runtime, mcp_user.user_id, "mon-2.png", d1 + timedelta(hours=1))
    c = await upload(api, runtime, mcp_user.user_id, "wed.png", d2)

    plan = await call("jolt_sync")
    assert plan["upload_dates"] == ["2026-10-20", "2026-10-22"]

    # The agent logs the whole backlog in one call.
    logged = await call(
        "jolt_log_session",
        source_ids=[a, b, c],
        concepts=[
            {"text": "Wednesday idea", "track_id": tid, "source_id": c},
            {"text": "Monday idea 1", "track_id": tid, "source_id": a},
            {"text": "Monday idea 2", "track_id": tid, "source_id": b},
            {"text": "No source given", "track_id": tid},  # goes with the first source
        ],
    )
    assert len(logged["session_ids"]) == 2
    assert logged["session_id"] == logged["session_ids"][0]
    sessions = {s.id: s for s in await runtime.repos.sessions.list_for_user(mcp_user.user_id)}
    monday, wednesday = (sessions[i] for i in logged["session_ids"])
    assert monday.upload_date == "2026-10-20" and wednesday.upload_date == "2026-10-22"
    assert monday.source_ids == [a, b] and wednesday.source_ids == [c]
    w, m1, m2, nosrc = logged["concept_ids"]  # input order is kept
    assert monday.concept_ids == [m1, m2, nosrc] and wednesday.concept_ids == [w]
    assert monday.sync_run_id == plan["run_id"]


async def test_one_upload_date_is_one_session(api, runtime, mcp_user):
    track = (await api.post("/v1/tracks", json={"name": "T"})).json()
    a = await upload(api, runtime, mcp_user.user_id, "1.png")
    b = await upload(api, runtime, mcp_user.user_id, "2.png")
    logged = await call(
        "jolt_log_session",
        source_ids=[a, b],
        concepts=[{"text": "x", "track_id": track["id"], "source_id": b}],
    )
    assert len(logged["session_ids"]) == 1
    # No sources at all still records one session (unchanged behaviour).
    empty = await call("jolt_log_session", source_ids=[], concepts=[])
    assert len(empty["session_ids"]) == 1 and empty["concept_ids"] == []


# --------------------------------------------------------------------------- #
# Sync runs
# --------------------------------------------------------------------------- #
async def test_a_full_run_is_recorded_with_counts_changes_and_first_sync(api, runtime, mcp_user):
    track = (await api.post("/v1/tracks", json={"name": "DS"})).json()
    tid = track["id"]
    when = datetime(2026, 10, 25, 22, 0, tzinfo=UTC)
    sid = await upload(api, runtime, mcp_user.user_id, "p.png", when)
    assert (await api.get("/v1/me")).json()["onboarding"]["first_sync_at"] is None

    plan = await call("jolt_sync")
    run_id = plan["run_id"]
    await call("jolt_get_unprocessed_sources", limit=plan["steps"][0]["limit"])
    await call("jolt_get_source_content", source_id=sid)
    await call("jolt_store_extraction", source_id=sid, markdown="# x", confidence=0.8)
    await call("jolt_store_extraction", source_id=sid, markdown="# x", supersede=True)  # retry
    logged = await call(
        "jolt_log_session",
        source_ids=[sid],
        concepts=[
            {"text": "Ring hashing", "title": "Ring", "track_id": tid, "concept_key": "ring"},
            {"text": "A long concept " * 10, "track_id": tid, "source_id": sid},
        ],
    )
    for cid in logged["concept_ids"]:
        await call("jolt_create_questions", concept_id=cid, questions=[QUESTION, QUESTION])
    await call("jolt_correct_concept", track_id=tid, concept_id=logged["concept_ids"][0], text="R")

    running = (await api.get("/v1/sync/status")).json()
    assert running["last_run"]["status"] == "running"
    assert running["last_run"]["finished_at"] is None

    final = await call("jolt_sync", errors=["page 2 unreadable", "  "])
    assert final["done"] is True and final["run_id"] == run_id and final["steps"] == []

    status = (await api.get("/v1/sync/status")).json()
    assert status["health"] == "ok"
    run = status["last_run"]
    assert run["id"] == run_id and run["status"] == "finished" and run["finished_at"]
    assert run["client_label"] == "Test agent"  # the MCP token's label
    assert (run["sources_processed"], run["questions_written"], run["gradings_submitted"]) == (
        1,
        4,
        0,
    )
    assert run["errors"] == ["page 2 unreadable"]
    added = [c for c in run["changes"] if c["kind"] == "concept_added"]
    assert [c["concept_id"] for c in added] == logged["concept_ids"]
    assert added[0]["concept_title"] == "Ring" and added[0]["added_to_syllabus"] is True
    assert added[1]["concept_title"].endswith("…") and added[1]["added_to_syllabus"] is False
    assert all(c["source_id"] == sid for c in added)  # one source in the call
    assert datetime.fromisoformat(added[0]["source_uploaded_at"]) == when
    updated = [c for c in run["changes"] if c["kind"] == "concept_updated"]
    assert [c["concept_id"] for c in updated] == [logged["concept_ids"][0]]

    me = (await api.get("/v1/me")).json()
    assert _dt(me["onboarding"]["first_sync_at"]) == _dt(run["finished_at"])

    # Checking again with nothing to do does not open another, empty run.
    again = await call("jolt_sync")
    assert again["done"] is True and again["run_id"] == run_id
    runs = (await api.get("/v1/sync/runs")).json()
    assert [r["id"] for r in runs] == [run_id]


async def test_runs_are_per_token_and_listed_newest_first(api, runtime, mcp_user):
    first = await call("jolt_sync")  # nothing to do: opened and finished at once
    codex = await AccountService(runtime).create_mcp_token(mcp_user.user_id, "Codex")
    await upload(api, runtime, mcp_user.user_id, "late.png")
    set_current_token(codex.token)
    second = await call("jolt_sync")
    set_current_token(mcp_user.token)
    assert second["run_id"] != first["run_id"]

    runs = (await api.get("/v1/sync/runs", params={"limit": 5})).json()
    assert [(r["client_label"], r["status"]) for r in runs] == [
        ("Codex", "running"),
        ("Test agent", "finished"),
    ]
    assert len((await api.get("/v1/sync/runs", params={"limit": 1})).json()) == 1
    assert (await api.get("/v1/sync/runs", params={"limit": 0})).status_code == 422
    # The empty run already counts as the first sync (the client connected).
    assert (await api.get("/v1/me")).json()["onboarding"]["first_sync_at"]


async def test_an_idle_run_is_closed_as_incomplete(api, runtime, mcp_user):
    track = (await api.post("/v1/tracks", json={"name": "T"})).json()
    sid = await upload(api, runtime, mcp_user.user_id, "a.png")
    plan = await call("jolt_sync")
    await call("jolt_get_unprocessed_sources")
    await call("jolt_store_extraction", source_id=sid, markdown="m")
    logged = await call(
        "jolt_log_session",
        source_ids=[sid],
        concepts=[{"text": "x", "track_id": track["id"], "source_id": sid}],
    )
    # The client goes quiet for longer than SYNC_RUN_IDLE_SECONDS.
    run = await runtime.repos.sync_runs.get(plan["run_id"], mcp_user.user_id)
    quiet_since = datetime.now(UTC) - timedelta(seconds=runtime.settings.sync_run_idle_seconds + 5)
    run.last_activity_at = quiet_since
    await runtime.repos.sync_runs.replace(run)

    # Late tool calls no longer count towards it.
    await call("jolt_create_questions", concept_id=logged["concept_ids"][0], questions=[QUESTION])
    status = (await api.get("/v1/sync/status")).json()
    last = status["last_run"]
    assert last["status"] == "incomplete" and last["questions_written"] == 0
    assert datetime.fromisoformat(last["finished_at"]) == quiet_since
    assert "without a final jolt_sync" in last["errors"][0]
    assert last["sources_processed"] == 1
    assert (await api.get("/v1/me")).json()["onboarding"]["first_sync_at"]  # it did work

    # The next jolt_sync opens a new run.
    assert (await call("jolt_sync"))["run_id"] != plan["run_id"]


async def test_tool_errors_are_recorded_on_the_run(api, runtime, mcp_user):
    await upload(api, runtime, mcp_user.user_id, "a.png")
    mcp = mcp_server.build_mcp()
    content = await mcp.call_tool("jolt_sync", {})
    plan_run = json.loads(content[0].text)["run_id"]
    with pytest.raises(Exception, match="source not found"):
        await mcp.call_tool("jolt_get_source_content", {"source_id": "nope"})
    run = await runtime.repos.sync_runs.get(plan_run, mcp_user.user_id)
    assert run.errors == ["jolt_get_source_content: source not found"]
    assert run.status == SyncRunStatus.RUNNING


async def test_tools_outside_a_run_record_nothing(api, runtime, mcp_user):
    track = (await api.post("/v1/tracks", json={"name": "T"})).json()
    await call("jolt_log_session", source_ids=[], concepts=[{"text": "x", "track_id": track["id"]}])
    assert (await api.get("/v1/sync/runs")).json() == []
    status = (await api.get("/v1/sync/status")).json()
    assert status["last_run"] is None and status["health"] == "never"


# --------------------------------------------------------------------------- #
# Status, health, prompt
# --------------------------------------------------------------------------- #
async def test_status_health_and_next_expected(api, runtime, mcp_user):
    never = (await api.get("/v1/sync/status", params={"tz": "Europe/London"})).json()
    assert never["health"] == "never" and never["last_run"] is None
    nxt = datetime.fromisoformat(never["next_expected_at"])
    local = nxt.astimezone(ZoneInfo("Europe/London"))
    assert (local.hour, local.minute) == (0, 15)
    assert timedelta(0) < nxt - datetime.now(UTC) <= timedelta(days=1)

    plan = await call("jolt_sync")
    assert (await api.get("/v1/sync/status")).json()["health"] == "ok"
    run = await runtime.repos.sync_runs.get(plan["run_id"], mcp_user.user_id)
    run.started_at = datetime.now(UTC) - timedelta(hours=31)
    await runtime.repos.sync_runs.replace(run)
    assert (await api.get("/v1/sync/status")).json()["health"] == "late"

    bad = await api.get("/v1/sync/status", params={"tz": "Mars/Olympus"})
    assert bad.status_code == 422 and "unknown time zone" in bad.json()["detail"]


def test_health_and_schedule_rules():
    now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
    late = timedelta(hours=30)
    assert sync_health(None, now, late) == "never"
    assert sync_health(now - timedelta(hours=29), now, late) == "ok"
    assert sync_health(now - timedelta(hours=31), now, late) == "late"
    at = parse_schedule_time("00:15")
    # 12:00 UTC is 21:00 in Tokyo: next 00:15 there is 15:15 UTC the same day.
    assert next_expected(now, at, ZoneInfo("Asia/Tokyo")) == datetime(
        2026, 10, 26, 15, 15, tzinfo=UTC
    )
    assert next_expected(now, at, ZoneInfo("UTC")) == datetime(2026, 10, 27, 0, 15, tzinfo=UTC)
    assert upload_day(datetime(2026, 10, 25, 23, 30, tzinfo=ZoneInfo("America/New_York"))) == (
        "2026-10-26"
    )


async def test_prompt_route_serves_the_doc(api):
    resp = (await api.get("/v1/sync/prompt")).json()
    expected = load_prompt()
    assert resp == {
        "prompt": expected.prompt,
        "schedule_time": "00:15",
        "version": expected.version,
    }
    assert "jolt_sync" in resp["prompt"] and "{{" not in resp["prompt"]
    assert "exactly 4" in resp["prompt"]
    # BE-07: corrections and supersede reconciliation are part of the night.
    for tool in (
        "jolt_get_pending_corrections",
        "jolt_correct_concept",
        "jolt_continue_reconciliation",
    ):
        assert tool in resp["prompt"], tool


def test_prompt_markers_are_required():
    with pytest.raises(ValueError):
        parse_prompt("no markers here")
    text = PROMPT_PATH.read_text()
    assert "<!-- prompt:start -->" in text and "<!-- prompt-version:" in text


def test_docker_image_ships_the_prompt():
    root = PROMPT_PATH.parent.parent
    assert "COPY docs/NIGHTLY_SYNC_PROMPT.md" in (root / "Dockerfile").read_text()
    assert "!docs/NIGHTLY_SYNC_PROMPT.md" in (root / ".dockerignore").read_text().splitlines()


async def test_sync_routes_need_an_app_token(client, runtime):
    for path in ("/v1/sync/status", "/v1/sync/runs", "/v1/sync/prompt"):
        assert client.get(path).status_code == 401


# --------------------------------------------------------------------------- #
# How /mcp takes the token
# --------------------------------------------------------------------------- #
async def test_event_stream_takes_the_token_from_the_query(client, runtime):
    me = client.post("/v1/auth/dev", json={"email": "q@example.com"}).json()
    auth = {"Authorization": f"Bearer {me['app_token']}"}
    token = client.post("/v1/mcp-tokens", json={"label": "Claude"}, headers=auth).json()["token"]

    # Wrong or missing query tokens are refused before the stream starts.
    for query, reason in (
        ("", "missing bearer token"),
        ("?token=jolt_live_nope", "invalid token"),
        (f"?token={me['app_token']}", "wrong token kind"),
    ):
        resp = client.get(f"/mcp/sse{query}")
        assert resp.status_code == 401 and reason in resp.json()["detail"]

    # A valid one passes the auth gate; the middleware hands it to the tools.
    seen = {}

    async def fake_app(scope, receive, send):
        from jolt.mcp.context import _current_token

        seen["token"] = _current_token.get()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = mcp_server._TokenCaptureMiddleware(fake_app)
    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "path": "/sse",
        "headers": [],
        "query_string": f"token={token}".encode(),
    }
    await middleware(scope, None, send)
    assert sent[0]["status"] == 204 and seen["token"] == token

    # The query is ignored off the event stream.
    scope = {
        "type": "http",
        "path": "/messages/",
        "headers": [],
        "query_string": f"token={token}".encode(),
    }
    await middleware(scope, None, send)
    assert seen["token"] is None


async def test_query_tokens_can_be_switched_off(settings, runtime):
    settings.mcp_query_token_enabled = False
    app = create_app(settings=settings, runtime=runtime)
    issued = await AccountService(runtime).sign_in_dev("off@example.com")
    mcp = await AccountService(runtime).create_mcp_token(issued.user.id, "Claude")
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        resp = c.get(f"/mcp/sse?token={mcp.token}")
        assert resp.status_code == 401 and "missing bearer token" in resp.json()["detail"]


def test_access_log_redacts_query_tokens(caplog):
    mcp_server.install_log_redaction()
    mcp_server.install_log_redaction()  # idempotent
    access = logging.getLogger("uvicorn.access")
    assert sum(isinstance(f, mcp_server.RedactTokenFilter) for f in access.filters) == 1
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", "/mcp/sse?token=jolt_live_secret&x=1", "1.1", 200),
        None,
    )
    for f in access.filters:
        f.filter(record)
    assert "jolt_live_secret" not in record.getMessage()
    assert "/mcp/sse?token=[redacted]&x=1" in record.getMessage()


# --------------------------------------------------------------------------- #
# BE-07 on the BE-04 plan: correction notes are sync work, reconciliation and
# corrections show up in the run's changes.
# --------------------------------------------------------------------------- #
async def test_pending_corrections_keep_the_plan_open_until_answered(api, runtime, mcp_user):
    track = (await api.post("/v1/tracks", json={"name": "T"})).json()
    sid = await upload(api, runtime, mcp_user.user_id, "a.png")
    run_id = (await call("jolt_sync"))["run_id"]
    await call("jolt_get_unprocessed_sources")
    await call("jolt_store_extraction", source_id=sid, markdown="m")
    (cid,) = (
        await call(
            "jolt_log_session",
            source_ids=[sid],
            concepts=[{"text": "old", "title": "Old", "track_id": track["id"], "source_id": sid}],
        )
    )["concept_ids"]
    await call("jolt_create_questions", concept_id=cid, questions=[QUESTION])
    assert (await call("jolt_sync"))["done"]

    note = await api.post(f"/v1/concepts/{cid}/correction", json={"note": "It is new."})
    assert note.is_success
    plan = await call("jolt_sync")
    assert not plan["done"] and plan["pending_corrections"] == 1
    # The finished run stays closed: the correction is new work, so a new run.
    assert plan["run_id"] != run_id
    tools = [s["tool"] for s in plan["steps"]]
    assert tools == ["jolt_get_pending_corrections", "jolt_correct_concept", "jolt_sync"]
    (line,) = plan["tracks"]
    assert (line["track_id"], line["pending_corrections"]) == (track["id"], 1)
    assert "1 concept(s) to correct" in plan["next"]

    (pending,) = (await call("jolt_get_pending_corrections"))["corrections"]
    assert pending["concept_id"] == cid
    fixed = await call("jolt_correct_concept", track_id=track["id"], concept_id=cid, text="new")
    assert (fixed["updated"], fixed["corrections_cleared"]) == (True, 1)
    final = await call("jolt_sync")
    assert final["done"] and final["pending_corrections"] == 0

    run = (await api.get("/v1/sync/status")).json()["last_run"]
    assert run["id"] == plan["run_id"] and run["status"] == "finished"
    assert [(c["kind"], c["concept_id"]) for c in run["changes"]] == [("concept_updated", cid)]


async def test_reconfirming_a_concept_clears_notes_without_a_change(api, runtime, mcp_user):
    track = (await api.post("/v1/tracks", json={"name": "T"})).json()
    await call("jolt_sync")
    (cid,) = (
        await call(
            "jolt_log_session", source_ids=[], concepts=[{"text": "x", "track_id": track["id"]}]
        )
    )["concept_ids"]
    await api.post(f"/v1/concepts/{cid}/correction", json={"note": "Check this."})
    fixed = await call("jolt_correct_concept", track_id=track["id"], concept_id=cid, text="x")
    assert fixed["corrections_cleared"] == 1
    plan = await call("jolt_sync")
    assert plan["done"]
    run = await runtime.repos.sync_runs.get(plan["run_id"], mcp_user.user_id)
    assert [c.kind.value for c in run.changes] == ["concept_added"]


async def test_reconciliation_adds_its_concepts_to_the_run(api, runtime, mcp_user):
    track = (await api.post("/v1/tracks", json={"name": "T"})).json()
    sid = await upload(api, runtime, mcp_user.user_id, "a.png")
    run_id = (await call("jolt_sync"))["run_id"]
    await call("jolt_get_unprocessed_sources")
    await call("jolt_store_extraction", source_id=sid, markdown="m")
    (cid,) = (
        await call(
            "jolt_log_session",
            source_ids=[sid],
            concepts=[
                {
                    "text": "Consistent hashing maps keys and servers onto one ring.",
                    "title": "Hashing",
                    "track_id": track["id"],
                    "source_id": sid,
                }
            ],
        )
    )["concept_ids"]
    assert (await api.post(f"/v1/sources/{sid}/reread", json={})).is_success
    await call("jolt_get_unprocessed_sources")
    stored = await call(
        "jolt_store_extraction",
        source_id=sid,
        markdown="m2",
        supersede=True,
        concepts=[
            {
                "text": "Consistent hashing maps keys and servers onto one ring.",
                "track_id": track["id"],
            },
            {
                "text": "Gossip spreads membership news from peer to peer.",
                "title": "Gossip",
                "track_id": track["id"],
            },
        ],
    )
    rec = stored["reconciliation"]
    assert [d["change"] for d in rec["deltas"]] == ["unchanged", "new"]
    new_id = rec["concept_ids"][1]
    # Resuming a finished reconciliation does not add the same change twice.
    await call("jolt_continue_reconciliation", source_id=sid)
    run = await runtime.repos.sync_runs.get(run_id, mcp_user.user_id)
    assert [(c.kind.value, c.concept_id) for c in run.changes] == [
        ("concept_added", cid),
        ("concept_added", new_id),
    ]
    assert run.changes[1].source_id == sid


async def test_log_session_refuses_another_users_source(api, runtime, mcp_user):
    track = (await api.post("/v1/tracks", json={"name": "T"})).json()
    with pytest.raises(Exception, match="source not found"):
        await call(
            "jolt_log_session",
            source_ids=["someone-elses"],
            concepts=[{"text": "x", "track_id": track["id"]}],
        )
    assert await runtime.repos.concept_states.all_for_user(mcp_user.user_id) == []
