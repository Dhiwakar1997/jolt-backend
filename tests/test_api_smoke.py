"""REST smoke tests over the in-memory fakes (decision A8).

These drive the real FastAPI app through a TestClient: routing, auth
dependencies, request/response models, services and repositories all run; only
Cosmos, Blob and Key Vault are replaced by `jolt/data/memory.py`.
"""

from __future__ import annotations

from jolt.credentials import providers
from jolt.credentials.providers import ValidationResult
from tests.conftest import dev_sign_in


def test_health(client):
    assert client.get("/health").json() == {"status": "ok", "version": "dev"}


def test_v1_requires_a_valid_bearer_token(client, account):
    assert client.get("/v1/tracks").status_code == 401
    bad = client.get("/v1/tracks", headers={"Authorization": "Bearer jolt_app_nope"})
    assert bad.status_code == 401
    assert bad.headers["www-authenticate"] == "Bearer"


def test_app_token_is_opaque_and_prefixed(account):
    assert account["token"].startswith("jolt_app_")
    assert account["user_id"] not in account["token"]


def test_create_and_list_tracks_with_agenda(client, auth):
    created = client.post(
        "/v1/tracks", json={"name": "Distributed Systems", "syllabus": ["hashing"]}, headers=auth
    )
    assert created.status_code == 200, created.text
    track = created.json()
    assert track["agenda"]["status"] == "refined"
    assert track["agenda"]["source"] == "user"
    assert track["syllabus"] == ["hashing"]

    listed = client.get("/v1/tracks", headers=auth).json()
    assert [t["id"] for t in listed] == [track["id"]]

    locked = client.post(f"/v1/tracks/{track['id']}/agenda/lock", headers=auth).json()
    assert locked["status"] == "locked"
    unlocked = client.post(f"/v1/tracks/{track['id']}/agenda/unlock", headers=auth).json()
    assert unlocked["status"] == "refined"

    cov = client.get(f"/v1/tracks/{track['id']}/coverage", headers=auth).json()
    assert cov["total_concepts"] == 0
    assert cov["uncovered_syllabus"] == ["hashing"]


def test_tracks_are_scoped_to_their_owner(client, auth):
    track = client.post("/v1/tracks", json={"name": "Mine"}, headers=auth).json()
    other = dev_sign_in(client)
    other_auth = {"Authorization": f"Bearer {other['token']}"}
    assert client.get("/v1/tracks", headers=other_auth).json() == []
    resp = client.post(f"/v1/tracks/{track['id']}/agenda/lock", headers=other_auth)
    assert resp.status_code == 404


def test_unknown_track_coverage_is_404(client, auth):
    assert client.get("/v1/tracks/nope/coverage", headers=auth).status_code == 404


def test_upload_request_confirm_and_list(client, auth, runtime):
    req = client.post(
        "/v1/uploads/request",
        json={"filename": "notes.jpg", "content_type": "image/jpeg", "sha256": "abc"},
        headers=auth,
    )
    assert req.status_code == 200, req.text
    ticket = req.json()
    assert ticket["upload_url"].startswith("memory://blob/")

    # Confirm before the bytes arrive -> 400.
    early = client.post(f"/v1/uploads/{ticket['source_id']}/confirm", json={}, headers=auth)
    assert early.status_code == 400

    # Simulate the client's direct PUT to Blob, then confirm.
    source_doc = runtime.cosmos.container("sources").all_items()[0]
    runtime.blob.put(source_doc["blob_path"], b"\xff\xd8 page bytes", "image/jpeg")
    mismatch = client.post(
        f"/v1/uploads/{ticket['source_id']}/confirm", json={"sha256": "zzz"}, headers=auth
    )
    assert mismatch.status_code == 400
    ok = client.post(
        f"/v1/uploads/{ticket['source_id']}/confirm", json={"sha256": "abc"}, headers=auth
    )
    assert ok.json() == {"status": "ready"}

    sources = client.get("/v1/sources", headers=auth).json()
    assert [(s["id"], s["processing_status"]) for s in sources] == [
        (ticket["source_id"], "unprocessed")
    ]
    history = client.get(f"/v1/sources/{ticket['source_id']}/extractions", headers=auth)
    assert history.json() == []


def test_due_is_empty_for_a_new_account(client, auth):
    assert client.get("/v1/reviews/due", headers=auth).json() == {"count": 0, "items": []}


def test_unknown_question_stage_one_is_404(client, auth):
    resp = client.post(
        "/v1/reviews/q1/stage1", json={"concept_id": "c1", "free_text": "x"}, headers=auth
    )
    assert resp.status_code == 404


def test_retention_round_trips(client, auth, runtime, account):
    resp = client.put("/v1/settings/retention", json={"retention_days": 30}, headers=auth)
    assert resp.json() == {"retention_days": 30}
    stored = runtime.cosmos.container("users").all_items()
    assert [u["retention_days"] for u in stored if u["id"] == account["user_id"]] == [30]


def test_api_key_capture_list_delete(client, auth, runtime, monkeypatch):
    async def fake_validate(self, key):  # no network in tests
        return ValidationResult(ok=key.startswith("sk-"), last4=key[-4:])

    monkeypatch.setattr(providers.AnthropicAdapter, "validate", fake_validate)

    bad = client.post(
        "/v1/settings/api-key", json={"provider": "anthropic", "key": "nope"}, headers=auth
    )
    assert bad.status_code == 422
    unsupported = client.post(
        "/v1/settings/api-key", json={"provider": "acme", "key": "sk-1"}, headers=auth
    )
    assert unsupported.status_code == 400

    ok = client.post(
        "/v1/settings/api-key",
        json={"provider": "anthropic", "key": "sk-test-1234"},
        headers=auth,
    )
    assert ok.json() == {"provider": "anthropic", "last4": "1234", "status": "validated"}
    assert len(runtime.vault.names()) == 1
    # Only a reference reaches Cosmos, never the key itself.
    refs = runtime.cosmos.container("api_keys").all_items()
    assert "sk-test-1234" not in str(refs)

    listed = client.get("/v1/settings/api-key", headers=auth).json()
    assert listed == [{"provider": "anthropic", "last4": "1234", "status": "stored"}]

    assert client.delete("/v1/settings/api-key/anthropic", headers=auth).status_code == 204
    assert runtime.vault.names() == []
    assert client.get("/v1/settings/api-key", headers=auth).json() == []
