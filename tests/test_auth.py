"""Accounts, Google sign-in, app and MCP tokens, /v1/me (BE-03).

Google ID tokens are minted with a test RSA key (`google_signer`) and verified by
the real `GoogleIdTokenVerifier`, so the signature, issuer, audience and expiry
checks all run without the network.
"""

from __future__ import annotations

import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from pydantic import ValidationError

from jolt.auth import context as auth_context
from jolt.auth.google import (
    GoogleIdTokenVerifier,
    GoogleJwks,
    GoogleKeysUnavailable,
    GoogleTokenError,
    StaticKeys,
)
from jolt.config import Settings
from jolt.credentials import providers
from jolt.credentials.providers import ValidationResult
from jolt.domain.models import user_id_for
from jolt.main import create_app
from jolt.mcp import server as mcp_server
from jolt.mcp.context import MCPAuthError, set_current_token
from jolt.runtime import Runtime
from jolt.services.accounts import AccountService
from tests.conftest import GOOGLE_CLIENT_ID, dev_sign_in

TOOLS = dict(mcp_server._TOOLS)


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Google sign-in
# --------------------------------------------------------------------------- #
def test_google_sign_in_happy_path(client, google_signer, runtime):
    resp = client.post("/v1/auth/google", json={"id_token": google_signer.mint()})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["app_token"].startswith("jolt_app_")
    user = body["user"]
    assert user["id"] == user_id_for("google", "1234567890")
    assert user["email"] == "ada@example.com"
    assert user["display_name"] == "Ada Lovelace"
    assert user["picture_url"] == "https://example.com/ada.png"
    assert user["created_at"]

    # The app token works on /v1 straight away.
    me = client.get("/v1/me", headers=bearer(body["app_token"]))
    assert me.status_code == 200
    assert me.json()["user"] == user

    # Only hashes are stored: neither the plaintext nor the Google token.
    docs = runtime.cosmos.container("tokens").all_items()
    assert len(docs) == 1 and docs[0]["kind"] == "app"
    assert body["app_token"] not in str(docs)


def test_signing_in_again_is_the_same_user_on_a_new_device(client, google_signer):
    first = client.post("/v1/auth/google", json={"id_token": google_signer.mint()}).json()
    again = client.post(
        "/v1/auth/google", json={"id_token": google_signer.mint(name="Ada King")}
    ).json()
    assert again["user"]["id"] == first["user"]["id"]
    assert again["user"]["display_name"] == "Ada King"  # profile refreshed
    assert again["app_token"] != first["app_token"]
    # Each device keeps its own token.
    for token in (first["app_token"], again["app_token"]):
        assert client.get("/v1/me", headers=bearer(token)).status_code == 200


def test_display_name_falls_back_to_the_email(client, google_signer):
    user = client.post(
        "/v1/auth/google", json={"id_token": google_signer.mint(sub="42", name=None)}
    ).json()["user"]
    assert user["display_name"] == "ada"


@pytest.mark.parametrize(
    ("claims", "reason"),
    [
        ({"aud": "someone-elses-client.apps.googleusercontent.com"}, "another client"),
        ({"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200}, "expired"),
        ({"iss": "https://evil.example.com"}, "not issued by Google"),
        ({"sub": None}, "invalid token"),
    ],
    ids=["bad-audience", "expired", "bad-issuer", "no-sub"],
)
def test_google_sign_in_rejects_bad_claims(client, google_signer, claims, reason):
    resp = client.post("/v1/auth/google", json={"id_token": google_signer.mint(**claims)})
    assert resp.status_code == 401, resp.text
    assert reason in resp.json()["detail"]
    assert resp.headers["www-authenticate"] == "Bearer"


def test_google_sign_in_rejects_a_forged_signature(client, google_signer):
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = google_signer.mint(key=other_key)
    resp = client.post("/v1/auth/google", json={"id_token": forged})
    assert resp.status_code == 401
    assert "bad signature" in resp.json()["detail"]


def test_google_sign_in_rejects_unknown_key_and_garbage(client, google_signer):
    unknown = google_signer.mint(headers={"kid": "rotated-away"})
    assert client.post("/v1/auth/google", json={"id_token": unknown}).status_code == 401
    assert client.post("/v1/auth/google", json={"id_token": "not.a.jwt"}).status_code == 401
    assert client.post("/v1/auth/google", json={"id_token": ""}).status_code == 422


def test_google_sign_in_rejects_a_symmetric_token(client):
    hs = jwt.encode({"sub": "1"}, "s" * 32, algorithm="HS256", headers={"kid": "test-kid"})
    resp = client.post("/v1/auth/google", json={"id_token": hs})
    assert resp.status_code == 401
    assert "algorithm" in resp.json()["detail"]


def test_google_sign_in_not_configured_is_503(google_signer):
    settings = Settings(_env_file=None, storage_mode="memory")
    with TestClient(create_app(settings=settings, runtime=Runtime.in_memory(settings))) as c:
        resp = c.post("/v1/auth/google", json={"id_token": google_signer.mint()})
    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"]


async def test_verifier_accepts_any_configured_client_id(google_signer):
    verifier = GoogleIdTokenVerifier(
        ["web.apps.googleusercontent.com", GOOGLE_CLIENT_ID],
        StaticKeys({google_signer.kid: google_signer.private_key.public_key()}),
    )
    identity = await verifier.verify(google_signer.mint(iss="accounts.google.com"))
    assert identity.sub == "1234567890" and identity.email_verified is True


# --------------------------------------------------------------------------- #
# Google JWKS fetching and caching (no network: httpx.MockTransport)
# --------------------------------------------------------------------------- #
def _jwks(google_signer, kid="test-kid") -> dict:
    jwk = json.loads(RSAAlgorithm.to_jwk(google_signer.private_key.public_key()))
    return {"keys": [{**jwk, "kid": kid, "alg": "RS256", "use": "sig"}]}


async def test_jwks_is_fetched_once_and_cached(google_signer):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(
            200, json=_jwks(google_signer), headers={"cache-control": "public, max-age=19000"}
        )

    jwks = GoogleJwks(transport=httpx.MockTransport(handler))
    verifier = GoogleIdTokenVerifier([GOOGLE_CLIENT_ID], jwks)
    for _ in range(3):
        assert (await verifier.verify(google_signer.mint())).sub == "1234567890"
    assert len(calls) == 1
    assert str(calls[0]) == "https://www.googleapis.com/oauth2/v3/certs"

    # An unknown kid refetches at most once per interval, then fails cleanly.
    with pytest.raises(GoogleTokenError):
        await jwks.get_key("rotated-in")
    assert len(calls) == 1  # fetched less than min_refetch_interval ago


async def test_jwks_unreachable_is_keys_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    jwks = GoogleJwks(transport=httpx.MockTransport(handler))
    with pytest.raises(GoogleKeysUnavailable):
        await jwks.get_key("any")


def test_google_keys_unavailable_is_503(client, runtime, google_signer):
    runtime.google = GoogleIdTokenVerifier(
        [GOOGLE_CLIENT_ID], GoogleJwks(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    )
    resp = client.post("/v1/auth/google", json={"id_token": google_signer.mint()})
    assert resp.status_code == 503


# --------------------------------------------------------------------------- #
# Dev sign-in
# --------------------------------------------------------------------------- #
def test_dev_sign_in_is_stable_per_email(client):
    a = dev_sign_in(client, "Grace@Example.com", display_name="Grace")
    b = dev_sign_in(client, "grace@example.com")
    assert a["user_id"] == b["user_id"] == user_id_for("dev", "grace@example.com")
    assert b["user"]["display_name"] == "Grace"
    assert client.post("/v1/auth/dev", json={"email": "not-an-email"}).status_code == 422


def test_dev_sign_in_is_not_mounted_by_default():
    settings = Settings(_env_file=None, storage_mode="memory")
    with TestClient(create_app(settings=settings, runtime=Runtime.in_memory(settings))) as c:
        assert c.post("/v1/auth/dev", json={"email": "a@b.c"}).status_code == 404


def test_dev_sign_in_checks_the_secret_when_set():
    settings = Settings(
        _env_file=None, storage_mode="memory", dev_auth_enabled=True, dev_auth_secret="s3cret"
    )
    with TestClient(create_app(settings=settings, runtime=Runtime.in_memory(settings))) as c:
        body = {"email": "a@b.c"}
        assert c.post("/v1/auth/dev", json=body).status_code == 403
        wrong = {"X-Jolt-Dev-Auth-Secret": "nope"}
        assert c.post("/v1/auth/dev", json=body, headers=wrong).status_code == 403
        right = {"X-Jolt-Dev-Auth-Secret": "s3cret"}
        assert c.post("/v1/auth/dev", json=body, headers=right).status_code == 200


def test_dev_sign_in_is_refused_by_config_where_unsafe():
    with pytest.raises(ValidationError, match="JOLT_ENV=prod"):
        Settings(_env_file=None, jolt_env="prod", dev_auth_enabled=True, dev_auth_secret="x")
    with pytest.raises(ValidationError, match="DEV_AUTH_SECRET"):
        Settings(_env_file=None, storage_mode="azure", dev_auth_enabled=True)
    Settings(_env_file=None, storage_mode="azure", dev_auth_enabled=True, dev_auth_secret="x")


# --------------------------------------------------------------------------- #
# Sign-out and token kinds
# --------------------------------------------------------------------------- #
def test_sign_out_revokes_only_the_current_app_token(client):
    phone = dev_sign_in(client, "two@example.com")
    laptop = dev_sign_in(client, "two@example.com")
    assert client.post("/v1/auth/sign-out", headers=bearer(phone["token"])).status_code == 204
    gone = client.get("/v1/me", headers=bearer(phone["token"]))
    assert gone.status_code == 401
    assert gone.json()["detail"] == "invalid token"
    assert client.get("/v1/me", headers=bearer(laptop["token"])).status_code == 200


def test_v1_rejects_an_mcp_token(client, auth):
    created = client.post("/v1/mcp-tokens", json={"label": "Claude Desktop"}, headers=auth).json()
    resp = client.get("/v1/me", headers=bearer(created["token"]))
    assert resp.status_code == 401
    assert "wrong token kind" in resp.json()["detail"]


async def test_mcp_rejects_an_app_token(mcp_user):
    set_current_token(mcp_user.app_token)
    with pytest.raises(MCPAuthError, match="wrong token kind"):
        await TOOLS["jolt_get_tracks"]()
    set_current_token(mcp_user.token)
    assert await TOOLS["jolt_get_tracks"]() == {"tracks": []}


def test_mcp_sse_refuses_a_bad_token_at_connect(client, auth):
    for headers in ({}, auth, bearer("jolt_live_nope")):
        resp = client.get("/mcp/sse", headers=headers)
        assert resp.status_code == 401, headers
        assert resp.headers["www-authenticate"] == "Bearer"
    assert "wrong token kind" in client.get("/mcp/sse", headers=auth).json()["detail"]


# --------------------------------------------------------------------------- #
# MCP tokens
# --------------------------------------------------------------------------- #
def test_mcp_token_create_list_revoke(client, auth, runtime):
    created = client.post("/v1/mcp-tokens", json={"label": "  Claude Desktop "}, headers=auth)
    assert created.status_code == 201, created.text
    tok = created.json()
    assert tok["token"].startswith("jolt_live_")
    assert tok["label"] == "Claude Desktop"
    assert tok["last4"] == tok["token"][-4:]
    assert tok["last_used_at"] is None

    client.post("/v1/mcp-tokens", json={"label": "Codex"}, headers=auth)
    listed = client.get("/v1/mcp-tokens", headers=auth).json()
    assert [t["label"] for t in listed] == ["Codex", "Claude Desktop"]  # newest first
    assert all(set(t) == {"id", "label", "last4", "created_at", "last_used_at"} for t in listed)
    # Shown once: the plaintext is in no later response and not stored.
    assert tok["token"] not in str(listed)
    assert tok["token"] not in str(runtime.cosmos.container("tokens").all_items())

    assert client.delete(f"/v1/mcp-tokens/{tok['id']}", headers=auth).status_code == 204
    assert [t["label"] for t in client.get("/v1/mcp-tokens", headers=auth).json()] == ["Codex"]
    assert client.delete(f"/v1/mcp-tokens/{tok['id']}", headers=auth).status_code == 404


def test_mcp_token_label_is_validated(client, auth):
    for label in ("", "   ", "x" * 61):
        resp = client.post("/v1/mcp-tokens", json={"label": label}, headers=auth)
        assert resp.status_code == 422, label


def test_mcp_tokens_are_scoped_to_their_owner(client, auth, account):
    tok = client.post("/v1/mcp-tokens", json={"label": "Mine"}, headers=auth).json()
    other = bearer(dev_sign_in(client)["token"])
    assert client.get("/v1/mcp-tokens", headers=other).json() == []
    assert client.delete(f"/v1/mcp-tokens/{tok['id']}", headers=other).status_code == 404
    # An app token's id cannot be revoked through the MCP token route.
    app_token_id = next(
        d["id"]
        for d in client.app.state.runtime.cosmos.container("tokens").all_items()
        if d["kind"] == "app" and d["user_id"] == account["user_id"]
    )
    assert client.delete(f"/v1/mcp-tokens/{app_token_id}", headers=auth).status_code == 404


async def test_mcp_token_revocation_takes_effect_immediately(settings, runtime, mcp_user):
    app = create_app(settings=settings, runtime=runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://t", headers=bearer(mcp_user.app_token)
    ) as api:
        assert await TOOLS["jolt_get_tracks"]() == {"tracks": []}
        [listed] = (await api.get("/v1/mcp-tokens")).json()
        assert listed["last_used_at"] is not None  # touched by the tool call
        assert (await api.delete(f"/v1/mcp-tokens/{listed['id']}")).status_code == 204
    with pytest.raises(MCPAuthError, match="invalid token"):
        await TOOLS["jolt_get_tracks"]()


async def test_token_lookup_verifies_one_hash_not_every_user(runtime, monkeypatch, mcp_user):
    service = AccountService(runtime)
    for i in range(5):
        signed_in = await service.sign_in_dev(f"crowd{i}@example.com")
        await service.create_mcp_token(signed_in.user.id, "client")

    calls = []
    real = auth_context.verify_token

    def counting(*args):
        calls.append(args)
        return real(*args)

    monkeypatch.setattr(auth_context, "verify_token", counting)
    assert await TOOLS["jolt_get_tracks"]() == {"tracks": []}
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# /v1/me
# --------------------------------------------------------------------------- #
def test_me_reports_onboarding_progress(client, auth, monkeypatch):
    me = client.get("/v1/me", headers=auth).json()
    assert set(me) == {"user", "onboarding", "mcp_server_url", "has_api_key"}
    assert me["onboarding"] == {"has_track": False, "has_mcp_token": False, "first_sync_at": None}
    assert me["has_api_key"] is False
    assert me["mcp_server_url"] == "http://testserver/mcp/sse"  # derived from the request

    client.post("/v1/tracks", json={"name": "Systems"}, headers=auth)
    client.post("/v1/mcp-tokens", json={"label": "Codex"}, headers=auth)

    async def fake_validate(self, key):  # no network in tests
        return ValidationResult(ok=True, last4=key[-4:])

    monkeypatch.setattr(providers.AnthropicAdapter, "validate", fake_validate)
    client.post(
        "/v1/settings/api-key", json={"provider": "anthropic", "key": "sk-1234"}, headers=auth
    )

    me = client.get("/v1/me", headers=auth).json()
    assert me["onboarding"] == {"has_track": True, "has_mcp_token": True, "first_sync_at": None}
    assert me["has_api_key"] is True


def test_me_requires_an_app_token(client):
    assert client.get("/v1/me").status_code == 401


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {"mcp_server_url": "https://mcp.jolt.example/mcp/sse"},
            "https://mcp.jolt.example/mcp/sse",
        ),
        ({"public_base_url": "http://localhost:8000/"}, "http://localhost:8000/mcp/sse"),
    ],
)
def test_me_mcp_server_url_comes_from_settings(overrides, expected):
    settings = Settings(_env_file=None, storage_mode="memory", dev_auth_enabled=True, **overrides)
    with TestClient(create_app(settings=settings, runtime=Runtime.in_memory(settings))) as c:
        token = dev_sign_in(c)["token"]
        assert c.get("/v1/me", headers=bearer(token)).json()["mcp_server_url"] == expected
