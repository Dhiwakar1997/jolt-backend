"""BE-05: multi-page uploads, source list / detail / past reads, read again,
failed sources and the storage figure, over the in-memory fakes (A8).

The app side runs through the real FastAPI app; the agent side through the MCP
tool functions, each result checked against its contract schema.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from jolt.domain.models import Source
from jolt.main import create_app
from jolt.mcp import server as mcp_server
from jolt.mcp.schemas import validate_output

TOOLS = dict(mcp_server._TOOLS)

PAGES = [b"\xff\xd8 page one", b"\xff\xd8 page two, longer", b"%PDF-1.7 page three"]
TYPES = ["image/jpeg", "image/jpeg", "application/pdf"]


async def call(name: str, **kwargs) -> dict:
    result = await TOOLS[name](**kwargs)
    validate_output(name, result)
    return result


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def page_specs(pages=PAGES, types=TYPES, names=None) -> list[dict]:
    names = names or ["image.jpg"] * len(pages)  # phones name every photo alike
    return [
        {"filename": n, "content_type": t, "size": len(d), "sha256": sha(d)}
        for n, t, d in zip(names, types, pages)
    ]


@pytest.fixture
async def api(settings, runtime, mcp_user):
    """Async REST client for the same user/runtime the MCP tools see."""
    app = create_app(settings=settings, runtime=runtime)
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {mcp_user.app_token}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        yield c


async def _source_doc(runtime, source_id: str) -> dict:
    return next(d for d in runtime.cosmos.container("sources").all_items() if d["id"] == source_id)


async def upload(api, runtime, track_id: str | None, *, title="Lecture 4", pages=PAGES):
    """Request, PUT every page (as the browser would), confirm. Returns the ticket."""
    body = {"title": title, "pages": page_specs(pages, TYPES[: len(pages)])}
    if track_id:
        body["track_id"] = track_id
    resp = await api.post("/v1/uploads/request", json=body)
    assert resp.status_code == 200, resp.text
    ticket = resp.json()
    doc = await _source_doc(runtime, ticket["source_id"])
    for page, data in zip(doc["pages"], pages):
        runtime.blob.put(page["blob_path"], data, page["content_type"])
    checks = [{"index": i, "sha256": sha(d), "size": len(d)} for i, d in enumerate(pages)]
    confirm = await api.post(f"/v1/uploads/{ticket['source_id']}/confirm", json={"pages": checks})
    assert confirm.json() == {"status": "ready"}, confirm.text
    return ticket


async def make_track(api, name="Distributed Systems") -> str:
    return (await api.post("/v1/tracks", json={"name": name})).json()["id"]


# --------------------------------------------------------------------------- #
# Upload request
# --------------------------------------------------------------------------- #
async def test_multi_page_request_returns_one_source_with_a_url_per_page(api, runtime):
    tid = await make_track(api)
    resp = await api.post(
        "/v1/uploads/request", json={"track_id": tid, "title": "Notes", "pages": page_specs()}
    )
    assert resp.status_code == 200, resp.text
    ticket = resp.json()
    assert [p["index"] for p in ticket["pages"]] == [0, 1, 2]
    urls = [p["upload_url"] for p in ticket["pages"]]
    assert len(set(urls)) == 3  # same filename, three different blobs
    assert ticket["upload_url"] == urls[0]
    assert ticket["max_bytes_per_file"] == 25 * 1024 * 1024
    assert ticket["accepted_types"] == [
        "image/jpeg",
        "image/png",
        "image/heic",
        "image/webp",
        "application/pdf",
    ]
    docs = runtime.cosmos.container("sources").all_items()
    assert len(docs) == 1  # decision A7: one source, N pages
    assert docs[0]["track_id"] == tid and docs[0]["title"] == "Notes"
    assert [p["blob_path"].rsplit("/", 1)[-1] for p in docs[0]["pages"]] == [
        "0-image.jpg",
        "1-image.jpg",
        "2-image.jpg",
    ]


async def test_upload_request_limits(api, runtime):
    tid = await make_track(api)

    def body(**page):
        spec = {**page_specs()[0], **page}
        return {"track_id": tid, "pages": [page_specs()[0], spec]}

    gif = await api.post("/v1/uploads/request", json=body(content_type="image/gif"))
    assert gif.status_code == 422
    assert gif.json()["detail"][0]["loc"] == ["body", "pages", 1, "content_type"]

    big = await api.post("/v1/uploads/request", json=body(size=25 * 1024 * 1024 + 1))
    assert big.status_code == 422
    assert big.json()["detail"][0]["loc"] == ["body", "pages", 1, "size"]

    bad_hash = await api.post("/v1/uploads/request", json=body(sha256="abc"))
    assert bad_hash.status_code == 422

    runtime.settings.upload_max_pages = 2
    many = await api.post("/v1/uploads/request", json={"pages": page_specs()})
    assert many.status_code == 422 and "at most 2 files" in many.json()["detail"][0]["msg"]

    neither = await api.post("/v1/uploads/request", json={"track_id": tid})
    assert neither.status_code == 422

    # Content types are case- and parameter-insensitive.
    ok = await api.post("/v1/uploads/request", json=body(content_type="Image/HEIC; q=1"))
    assert ok.status_code == 200, ok.text


async def test_upload_to_someone_elses_track_is_404(api, client):
    from tests.conftest import dev_sign_in

    other = dev_sign_in(client)
    theirs = client.post(
        "/v1/tracks", json={"name": "Theirs"}, headers={"Authorization": f"Bearer {other['token']}"}
    ).json()["id"]
    resp = await api.post("/v1/uploads/request", json={"track_id": theirs, "pages": page_specs()})
    assert resp.status_code == 404
    missing = await api.post(
        "/v1/uploads/request", json={"track_id": "nope", "pages": page_specs()}
    )
    assert missing.status_code == 404


# --------------------------------------------------------------------------- #
# Confirm
# --------------------------------------------------------------------------- #
async def test_confirm_checks_every_page(api, runtime):
    tid = await make_track(api)
    ticket = (
        await api.post("/v1/uploads/request", json={"track_id": tid, "pages": page_specs()})
    ).json()
    sid = ticket["source_id"]
    doc = await _source_doc(runtime, sid)
    paths = [p["blob_path"] for p in doc["pages"]]

    async def confirm(**body):
        return await api.post(f"/v1/uploads/{sid}/confirm", json=body)

    runtime.blob.put(paths[0], PAGES[0])
    runtime.blob.put(paths[1], PAGES[1])
    missing = await confirm()
    assert missing.status_code == 400 and "page 3" in missing.json()["detail"]

    runtime.blob.put(paths[2], b"%PDF-1.7 page THREE")  # same size, other bytes
    wrong = await confirm()
    assert wrong.status_code == 400
    assert wrong.json()["detail"].startswith("sha256 mismatch on page 3")

    runtime.blob.put(paths[2], PAGES[2] + b"!")
    short = await confirm()
    assert short.status_code == 400 and "size mismatch on page 3" in short.json()["detail"]

    runtime.blob.put(paths[2], PAGES[2])
    claim = await confirm(pages=[{"index": 1, "sha256": sha(PAGES[0]), "size": len(PAGES[1])}])
    assert claim.status_code == 400 and "between request and confirm" in claim.json()["detail"]

    # Not visible to the app before confirm.
    assert (await api.get("/v1/sources")).json() == []
    assert (await api.get(f"/v1/sources/{sid}")).status_code == 404
    assert (await api.get("/v1/storage")).json() == {
        "bytes_used": 0,
        "source_count": 0,
        "page_count": 0,
    }

    assert (await confirm()).json() == {"status": "ready"}
    assert (await confirm()).json() == {"status": "ready"}  # idempotent
    stored = await _source_doc(runtime, sid)
    assert stored["size_bytes"] == sum(len(p) for p in PAGES)
    assert [p["size_bytes"] for p in stored["pages"]] == [len(p) for p in PAGES]


async def test_confirm_drops_an_oversize_file(api, runtime):
    runtime.settings.upload_max_bytes = 8
    data = b"0123456789abcdef"
    ticket = (
        await api.post(
            "/v1/uploads/request",
            json={"filename": "a.png", "content_type": "image/png", "sha256": sha(data)},
        )
    ).json()
    path = (await _source_doc(runtime, ticket["source_id"]))["blob_path"]
    runtime.blob.put(path, data)
    resp = await api.post(f"/v1/uploads/{ticket['source_id']}/confirm", json={})
    assert resp.status_code == 400 and "larger than 8 bytes" in resp.json()["detail"]
    assert runtime.blob.get(path) is None


# --------------------------------------------------------------------------- #
# The loop: unprocessed -> sync -> processed, detail, read again
# --------------------------------------------------------------------------- #
async def test_upload_sync_detail_and_read_again(api, runtime, mcp_user):
    tid = await make_track(api)
    ticket = await upload(api, runtime, tid, pages=PAGES[:2])
    sid = ticket["source_id"]

    (row,) = (await api.get("/v1/sources")).json()
    assert row["processing_status"] == "unprocessed"
    assert row["title"] == "Lecture 4" and row["track_id"] == tid
    assert row["track_name"] == "Distributed Systems"
    assert row["kind"] == "photos" and row["page_count"] == 2
    assert (row["concepts_found"], row["questions_written"], row["low_confidence"]) == (0, 0, False)
    assert row["reread_requested"] is None
    assert row["read_count"] == 0
    assert (await api.get("/v1/storage")).json() == {
        "bytes_used": len(PAGES[0]) + len(PAGES[1]),
        "source_count": 1,
        "page_count": 2,
    }

    # Agent: plan, claim, read every page.
    plan = await call("jolt_sync")
    assert (plan["unprocessed_sources"], plan["pending_gradings"]) == (1, 0)
    assert (plan["rereads"], plan["reread_sources"]) == (0, [])
    (claimed,) = (await call("jolt_get_unprocessed_sources"))["sources"]
    assert claimed["source_id"] == sid and claimed["page_count"] == 2
    assert claimed["track_id"] == tid and claimed["title"] == "Lecture 4"
    assert claimed["supersede"] is False and claimed["preferred_model"] is None
    assert (await api.get("/v1/sources", params={"status": "in_flight"})).json()[0]["id"] == sid

    content = await call("jolt_get_source_content", source_id=sid)
    assert content["page_count"] == 2
    assert [p["index"] for p in content["pages"]] == [0, 1]
    assert content["read_url"] == content["pages"][0]["read_url"]
    assert all("sp=r" in p["read_url"] for p in content["pages"])

    first = await call(
        "jolt_store_extraction",
        source_id=sid,
        markdown="# Hashing\nRing.",
        confidence=0.4,
        model_id="claude-x",
    )
    logged = await call(
        "jolt_log_session",
        source_ids=[sid],
        concepts=[
            {
                "text": "Consistent hashing uses a ring.",
                "title": "Consistent hashing",
                "track_id": tid,
                "source_id": sid,
            },
            {"text": "Virtual nodes even out load.", "track_id": tid, "source_id": sid},
        ],
    )
    c1, _c2 = logged["concept_ids"]
    q = {"stem": "?", "options": ["a", "b", "c", "d"], "correct_index": 0, "expected_answer": "a"}
    await call("jolt_create_questions", concept_id=c1, questions=[q, q])

    (row,) = (await api.get("/v1/sources")).json()
    assert row["processing_status"] == "processed"
    assert row["active_extraction_id"] == first["extraction_id"]
    assert (row["concepts_found"], row["questions_written"]) == (2, 2)
    assert row["low_confidence"] is True  # 0.4 < 0.6: "Unsure about the handwriting"
    assert row["read_count"] == 1

    detail = (await api.get(f"/v1/sources/{sid}")).json()
    assert [(p["index"], p["content_type"], p["size"]) for p in detail["pages"]] == [
        (0, "image/jpeg", len(PAGES[0])),
        (1, "image/jpeg", len(PAGES[1])),
    ]
    assert all("sp=r" in p["url"] for p in detail["pages"])
    active = detail["active_read"]
    assert active["markdown"] == "# Hashing\nRing."
    assert active["number"] == 1 and active["status"] == "active"
    assert active["model_id"] == "claude-x" and active["client_label"] == "Test agent"
    assert [c["title"] for c in active["concepts"]] == [
        "Consistent hashing",
        "Virtual nodes even out load.",
    ]
    assert all(c["is_new"] for c in active["concepts"])
    assert [r["id"] for r in detail["reads"]] == [first["extraction_id"]]

    # App: read again, preferring Codex.
    rr = await api.post(f"/v1/sources/{sid}/reread", json={"preferred_model": "codex"})
    assert rr.status_code == 200, rr.text
    assert rr.json()["preferred_model"] == "codex" and rr.json()["source_id"] == sid
    (row,) = (await api.get("/v1/sources")).json()
    assert row["processing_status"] == "unprocessed"
    assert row["reread_requested"]["preferred_model"] == "codex"

    # The next sync's plan lists it with the preferred model.
    plan = await call("jolt_sync")
    assert plan["unprocessed_sources"] == 1 and plan["rereads"] == 1
    (reread,) = plan["reread_sources"]
    assert (reread["source_id"], reread["preferred_model"]) == (sid, "codex")
    assert (reread["title"], reread["track_id"]) == ("Lecture 4", tid)
    tools = [st["tool"] for st in plan["steps"]]
    assert tools.index("jolt_diff_extractions") < tools.index("jolt_store_extraction")
    (line,) = plan["tracks"]
    assert (line["track_id"], line["unprocessed_sources"], line["rereads"]) == (tid, 1, 1)
    (claimed,) = (await call("jolt_get_unprocessed_sources"))["sources"]
    assert claimed["supersede"] is True and claimed["preferred_model"] == "codex"

    second = await call(
        "jolt_store_extraction",
        source_id=sid,
        markdown="# Hashing, better read",
        confidence=0.95,
        model_id="codex-y",
        supersede=True,
    )
    await call(
        "jolt_log_session",
        source_ids=[sid],
        concepts=[{"text": "Consistent hashing, clearer.", "track_id": tid, "source_id": sid}],
    )

    detail = (await api.get(f"/v1/sources/{sid}")).json()
    assert detail["processing_status"] == "processed"
    assert detail["reread_requested"] is None
    assert detail["low_confidence"] is False
    assert detail["read_count"] == 2
    assert (await api.get("/v1/sources")).json()[0]["read_count"] == 2
    assert detail["active_read"]["id"] == second["extraction_id"]
    assert detail["active_read"]["markdown"] == "# Hashing, better read"
    assert [
        (r["number"], r["status"], r["concepts_found"], r["questions_written"])
        for r in detail["reads"]
    ] == [(2, "active", 1, 0), (1, "superseded", 2, 2)]
    history = (await api.get(f"/v1/sources/{sid}/extractions")).json()
    assert [h["status"] for h in history] == ["active", "superseded"]
    final = await call("jolt_sync")
    assert (final["rereads"], final["reread_sources"], final["done"]) == (0, [], True)


async def test_read_again_while_a_sync_holds_the_source_waits_for_the_next_sync(api, runtime):
    ticket = await upload(api, runtime, None, pages=PAGES[:1])
    sid = ticket["source_id"]
    await call("jolt_get_unprocessed_sources")  # claimed, in flight
    rr = await api.post(f"/v1/sources/{sid}/reread", json={})
    assert rr.status_code == 200 and rr.json()["preferred_model"] is None
    (row,) = (await api.get("/v1/sources")).json()
    assert row["processing_status"] == "in_flight"  # the running sync keeps its lease

    await call("jolt_store_extraction", source_id=sid, markdown="first", confidence=0.9)
    (row,) = (await api.get("/v1/sources")).json()
    assert row["processing_status"] == "unprocessed"  # the request still stands
    assert row["reread_requested"] is not None
    plan = await call("jolt_sync")
    assert [r["source_id"] for r in plan["reread_sources"]] == [sid]


async def test_lapsed_leases_mark_a_source_failed_and_read_again_revives_it(api, runtime):
    runtime.settings.source_max_attempts = 2
    sid = (await upload(api, runtime, None, pages=PAGES[:1]))["source_id"]
    repo = runtime.repos.sources
    owner = (await _source_doc(runtime, sid))["user_id"]

    async def lapse():
        src = await repo.get_source(sid, owner)
        src.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await repo.replace(src)

    for _ in range(2):
        assert len((await call("jolt_get_unprocessed_sources"))["sources"]) == 1
        await lapse()
    assert (await call("jolt_sync"))["unprocessed_sources"] == 0
    assert (await call("jolt_get_unprocessed_sources"))["sources"] == []
    (row,) = (await api.get("/v1/sources", params={"status": "failed"})).json()
    assert row["id"] == sid
    listed = (await call("jolt_list_sources"))["sources"]
    assert listed[0]["processing_status"] == "failed"

    await api.post(f"/v1/sources/{sid}/reread", json={"preferred_model": "claude"})
    assert (await api.get("/v1/sources", params={"status": "failed"})).json() == []
    (claimed,) = (await call("jolt_get_unprocessed_sources"))["sources"]
    assert claimed["source_id"] == sid and claimed["supersede"] is False


async def test_filters_scope_and_unknown_sources(api, runtime, client):
    from tests.conftest import dev_sign_in

    t1, t2 = await make_track(api, "One"), await make_track(api, "Two")
    s1 = (await upload(api, runtime, t1, title="a", pages=PAGES[:1]))["source_id"]
    s2 = (await upload(api, runtime, t2, title="b", pages=PAGES[1:2]))["source_id"]
    assert [s["id"] for s in (await api.get("/v1/sources")).json()] == [s2, s1]  # newest first
    assert [s["id"] for s in (await api.get("/v1/sources", params={"track_id": t1})).json()] == [s1]
    assert (await api.get("/v1/sources", params={"status": "processed"})).json() == []
    assert (await api.get("/v1/sources", params={"status": "done"})).status_code == 422

    other = {"Authorization": f"Bearer {dev_sign_in(client)['token']}"}
    assert client.get("/v1/sources", headers=other).json() == []
    assert client.get(f"/v1/sources/{s1}", headers=other).status_code == 404
    assert client.post(f"/v1/sources/{s1}/reread", json={}, headers=other).status_code == 404
    assert client.get("/v1/storage", headers=other).json()["bytes_used"] == 0
    assert (await api.get("/v1/sources/nope")).status_code == 404


async def test_a_single_file_source_written_before_be05_still_reads(api, runtime, mcp_user):
    data = b"old page"
    path = f"{mcp_user.user_id}/legacy/notes.png"
    runtime.blob.put(path, data, "image/png")
    legacy = Source(
        user_id=mcp_user.user_id,
        filename="notes.png",
        content_type="image/png",
        sha256=sha(data),
        blob_path=path,
        size_bytes=len(data),
        confirmed_at=datetime.now(timezone.utc),
    )
    await runtime.repos.sources.create(legacy)
    detail = (await api.get(f"/v1/sources/{legacy.id}")).json()
    assert detail["title"] == "notes.png" and detail["page_count"] == 1
    assert detail["track_id"] is None and detail["kind"] == "photos"
    assert [(p["index"], p["size"]) for p in detail["pages"]] == [(0, len(data))]
    content = await call("jolt_get_source_content", source_id=legacy.id)
    assert content["pages"][0]["read_url"] == content["read_url"]
    assert (await api.get("/v1/storage")).json()["bytes_used"] == len(data)
