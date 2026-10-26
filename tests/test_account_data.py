"""BE-08: export everything, delete everything.

The delete test builds a user with something in every store (tokens, sync runs,
sources with page blobs and reads, concepts with question versions, FSRS cards,
reviews with gradings, review and study sessions, an API key in Key Vault, an
export file), deletes the account, then scans every in-memory container, the
blob store and the vault for anything left of that user. A second user's data
must survive untouched.
"""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from jolt.credentials import providers
from jolt.credentials.providers import ValidationResult
from jolt.data.cosmos import CONTAINER_PARTITION_KEYS
from jolt.domain.models import AnswerFeedback
from jolt.main import create_app
from jolt.mcp import context as mcp_context
from jolt.mcp import server as mcp_server
from jolt.mcp.schemas import validate_output
from jolt.services.accounts import AccountService

TOOLS = dict(mcp_server._TOOLS)
PAGES = [b"\xff\xd8 page one", b"%PDF-1.7 page two"]
TYPES = ["image/jpeg", "application/pdf"]


async def call(name: str, **kwargs) -> dict:
    result = await TOOLS[name](**kwargs)
    validate_output(name, result)
    return result


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(autouse=True)
def no_network_key_checks(monkeypatch):
    async def fake_validate(self, key, **_):
        return ValidationResult(ok=True, last4=key[-4:])

    monkeypatch.setattr(providers.AnthropicAdapter, "validate", fake_validate)


async def ok(resp: httpx.Response, status: int = 200):
    assert resp.status_code == status, resp.text
    return resp.json() if resp.content else None


async def populate(runtime, settings, email: str) -> dict:
    """A user with data in every container, made through the real routes and tools."""
    service = AccountService(runtime)
    signed = await service.sign_in_dev(email)
    issued = await service.create_mcp_token(signed.user.id, "Claude Desktop")
    mcp_context.set_runtime(runtime)
    mcp_context.set_current_token(issued.token)
    uid = signed.user.id
    app = create_app(settings=settings, runtime=runtime)
    headers = {"Authorization": f"Bearer {signed.app_token}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as api:
        track_id = (await ok(await api.post("/v1/tracks", json={"name": "Systems"})))["id"]
        pages = [
            {"filename": f"p{i}", "content_type": t, "size": len(d), "sha256": sha(d)}
            for i, (t, d) in enumerate(zip(TYPES, PAGES))
        ]
        ticket = await ok(
            await api.post("/v1/uploads/request", json={"track_id": track_id, "pages": pages})
        )
        source_id = ticket["source_id"]
        doc = next(
            d for d in runtime.cosmos.container("sources").all_items() if d["id"] == source_id
        )
        for page, data in zip(doc["pages"], PAGES):
            runtime.blob.put(page["blob_path"], data, page["content_type"])
        checks = [{"index": i, "sha256": sha(d), "size": len(d)} for i, d in enumerate(PAGES)]
        await ok(await api.post(f"/v1/uploads/{source_id}/confirm", json={"pages": checks}))
        # An abandoned upload: requested, bytes PUT, never confirmed.
        lost = await ok(
            await api.post("/v1/uploads/request", json={"track_id": track_id, "pages": pages[:1]})
        )
        lost_doc = next(
            d
            for d in runtime.cosmos.container("sources").all_items()
            if d["id"] == lost["source_id"]
        )
        runtime.blob.put(lost_doc["pages"][0]["blob_path"], PAGES[0], TYPES[0])

        # The nightly sync: read, concepts, questions.
        await call("jolt_sync")
        await call("jolt_get_unprocessed_sources")
        await call("jolt_store_extraction", source_id=source_id, markdown="# Notes", confidence=0.9)
        logged = await call(
            "jolt_log_session",
            source_ids=[source_id],
            concepts=[
                {
                    "text": "Hashing",
                    "title": "Hashing",
                    "track_id": track_id,
                    "source_id": source_id,
                }
            ],
        )
        (concept_id,) = logged["concept_ids"]
        q = {
            "stem": "Why?",
            "options": ["a", "b", "c", "d"],
            "correct_index": 0,
            "expected_answer": "a",
        }
        await call("jolt_create_questions", concept_id=concept_id, questions=[q])

        # A review session with an answer, graded with a rewrite (question v2).
        await ok(await api.patch("/v1/settings", json={"allow_question_rewrite": True}))
        session = await ok(await api.post("/v1/review-sessions", json={}))
        item = session["items"][0]
        s1 = await ok(
            await api.post(
                f"/v1/reviews/{item['question_id']}/stage1",
                json={
                    "concept_id": concept_id,
                    "free_text": "because",
                    "session_id": session["id"],
                },
            )
        )
        await ok(
            await api.post(f"/v1/reviews/{s1['review_id']}/stage2", json={"selected_index": 0})
        )
        await ok(await api.post(f"/v1/review-sessions/{session['id']}/end"))
        await call(
            "jolt_submit_gradings",
            gradings=[
                {
                    "review_id": s1["review_id"],
                    "suggested_fsrs_grade": 3,
                    "rationale": "Good",
                    "rewritten_question": {**q, "stem": "Why, exactly?"},
                }
            ],
        )
        await call("jolt_sync")  # closes the sync run
        # answer_feedback has no writer yet; put one in so the delete must reach it.
        await runtime.repos.answer_feedback.create(
            AnswerFeedback(review_id=s1["review_id"], user_id=uid, suggested_fsrs_grade=3)
        )
        await ok(
            await api.post(
                "/v1/settings/api-key", json={"provider": "anthropic", "key": "sk-ant-9876"}
            )
        )
        export = await ok(await api.post("/v1/export"))
    mcp_context.set_current_token(None)
    return {
        "uid": uid,
        "app_token": signed.app_token,
        "mcp_token": issued.token,
        "track_id": track_id,
        "source_id": source_id,
        "concept_id": concept_id,
        "review_id": s1["review_id"],
        "export": export,
    }


def _docs_of(runtime, data: dict) -> dict[str, list[dict]]:
    """Every stored document that belongs to the user, by container."""
    owned_ids = {
        data["uid"],
        data["track_id"],
        data["source_id"],
        data["concept_id"],
        data["review_id"],
    }
    found: dict[str, list[dict]] = {}
    for name in CONTAINER_PARTITION_KEYS:
        for doc in runtime.cosmos.container(name).all_items():
            text = json.dumps(doc)
            if any(i in text for i in owned_ids):
                found.setdefault(name, []).append(doc)
    return found


async def test_export_bundle_has_everything_and_no_secrets(runtime, settings):
    data = await populate(runtime, settings, "export@example.com")
    export = data["export"]
    assert set(export) == {"download_url", "expires_at", "bytes"}
    (path,) = [p for p in runtime.blob.paths() if p.startswith(f"exports/{data['uid']}/")]
    stored = runtime.blob.get(path)
    assert stored.content_type == "application/json"
    assert len(stored.data) == export["bytes"]
    bundle = json.loads(stored.data)
    assert bundle["format"] == "jolt-export" and bundle["format_version"] == 1
    assert bundle["user"]["id"] == data["uid"]
    assert [t["name"] for t in bundle["tracks"]] == ["Systems"]
    assert [c["id"] for c in bundle["concepts"]] == [data["concept_id"]]
    assert sorted(q["version"] for q in bundle["questions"]) == [1, 2]
    assert [r["id"] for r in bundle["reviews"]] == [data["review_id"]]
    assert bundle["reviews"][0]["free_text"] == "because"
    assert len(bundle["concept_states"]) == 1
    assert len(bundle["review_sessions"]) == 1 and len(bundle["sessions"]) == 1
    assert {s["id"] for s in bundle["sources"]} >= {data["source_id"]}
    assert [e["markdown"] for e in bundle["extractions"]] == ["# Notes"]
    assert len(bundle["sync_runs"]) == 1 and bundle["sync_runs"][0]["status"] == "finished"
    assert bundle["user"]["settings"]["allow_question_rewrite"] is True
    assert [(k["provider"], k["last4"]) for k in bundle["api_keys"]] == [("anthropic", "9876")]
    # Metadata only: no token, key, hash, Key Vault reference, blob path or etag.
    text = stored.data.decode()
    secrets = (data["app_token"], data["mcp_token"], "sk-ant-9876")
    internals = ("token_hash", "lookup", "key_ref", "blob_path", "_etag")
    for needle in secrets + internals:
        assert needle not in text, needle
    token_fields = {"id", "kind", "label", "last4", "created_at", "last_used_at"}
    assert all(set(t) == token_fields for t in bundle["mcp_tokens"])


async def test_a_new_export_replaces_the_previous_file(runtime, settings, client):
    data = await populate(runtime, settings, "twice@example.com")
    headers = {"Authorization": f"Bearer {data['app_token']}"}
    first = [p for p in runtime.blob.paths() if p.startswith("exports/")]
    body = client.post("/v1/export", headers=headers).json()
    second = [p for p in runtime.blob.paths() if p.startswith("exports/")]
    assert len(first) == len(second) == 1 and first != second
    assert body["download_url"]
    assert client.post("/v1/export").status_code == 401


async def test_export_downloads_over_the_dev_blob_route(settings, runtime):
    """With PUBLIC_BASE_URL set (memory mode) the signed URL is served for real."""
    from jolt.config import Settings
    from jolt.runtime import Runtime

    cfg = Settings(
        _env_file=None, storage_mode="memory", dev_auth_enabled=True, public_base_url="http://t"
    )
    rt = Runtime.in_memory(cfg)
    app = create_app(settings=cfg, runtime=rt)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        signed = await ok(await c.post("/v1/auth/dev", json={"email": "dl@example.com"}))
        headers = {"Authorization": f"Bearer {signed['app_token']}"}
        export = await ok(await c.post("/v1/export", headers=headers))
        assert export["download_url"].startswith("http://t/_dev/blob/")
        download = await c.get(export["download_url"])
        assert download.status_code == 200
        assert download.json()["user"]["id"] == signed["user"]["id"]


async def test_delete_account_leaves_nothing_behind(runtime, settings, client):
    victim = await populate(runtime, settings, "leaving@example.com")
    keeper = await populate(runtime, settings, "staying@example.com")
    uid = victim["uid"]
    headers = {"Authorization": f"Bearer {victim['app_token']}"}

    before = _docs_of(runtime, victim)
    # Something of the user's is in every container before the delete.
    assert set(before) == set(CONTAINER_PARTITION_KEYS), set(CONTAINER_PARTITION_KEYS) - set(before)
    assert any(p.startswith(f"{uid}/") for p in runtime.blob.paths())
    assert any(uid in n for n in runtime.vault.names())
    keeper_before = _docs_of(runtime, keeper)
    keeper_blobs = [p for p in runtime.blob.paths() if keeper["uid"] in p]

    # The confirmation field is required, and must be exactly "DELETE".
    for body in (None, {}, {"confirm": "delete"}, {"confirm": "yes"}):
        resp = client.request("DELETE", "/v1/me", json=body, headers=headers)
        assert resp.status_code == 422, resp.text
    assert client.get("/v1/me", headers=headers).status_code == 200

    resp = client.request("DELETE", "/v1/me", json={"confirm": "DELETE"}, headers=headers)
    assert resp.status_code == 204, resp.text

    assert _docs_of(runtime, victim) == {}
    assert [p for p in runtime.blob.paths() if uid in p] == []
    assert [n for n in runtime.vault.names() if uid in n] == []
    # Signed out everywhere, MCP cut off.
    assert client.get("/v1/me", headers=headers).status_code == 401
    mcp_context.set_current_token(victim["mcp_token"])
    with pytest.raises(Exception):
        await TOOLS["jolt_sync"]()
    mcp_context.set_current_token(None)

    # The other user is untouched.
    assert _docs_of(runtime, keeper) == keeper_before
    assert [p for p in runtime.blob.paths() if keeper["uid"] in p] == keeper_blobs
    assert any(keeper["uid"] in n for n in runtime.vault.names())
    other = {"Authorization": f"Bearer {keeper['app_token']}"}
    assert client.get("/v1/me", headers=other).status_code == 200

    # Signing in again with the same e-mail starts from nothing.
    again = client.post("/v1/auth/dev", json={"email": "leaving@example.com"}).json()
    assert again["user"]["id"] == uid
    me = client.get("/v1/me", headers={"Authorization": f"Bearer {again['app_token']}"}).json()
    assert me["onboarding"] == {"has_track": False, "has_mcp_token": False, "first_sync_at": None}
    assert me["has_api_key"] is False


async def test_delete_keeps_curated_tracks(runtime, settings, client):
    from jolt.domain.models import Concept, ConceptState, Track, TrackOrigin

    data = await populate(runtime, settings, "curated@example.com")
    curated = await runtime.repos.tracks.create(Track(name="DSA", origin=TrackOrigin.JOLT))
    concept = await runtime.repos.concepts.create(Concept(track_id=curated.id, text="Two pointers"))
    await runtime.repos.concept_states.create(
        ConceptState(user_id=data["uid"], concept_id=concept.id, track_id=curated.id)
    )
    headers = {"Authorization": f"Bearer {data['app_token']}"}
    resp = client.request("DELETE", "/v1/me", json={"confirm": "DELETE"}, headers=headers)
    assert resp.status_code == 204
    assert await runtime.repos.tracks.get_track(curated.id, None) is not None
    assert await runtime.repos.concepts.get_concept(concept.id, curated.id) is not None
    assert await runtime.repos.concept_states.for_concept(data["uid"], concept.id) is None
