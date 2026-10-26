"""Shared fixtures: the real app wired to in-memory Cosmos/Blob/Key Vault (A8).

`client` is a FastAPI TestClient over `create_app(...)` with an injected
in-memory `Runtime`, so REST routes run end to end with no Azure. Google sign-in
is verified against a test RSA key (`google_signer` mints ID tokens), so nothing
touches the network. `account` signs a user in through the dev bypass and holds
their **app token**; `mcp_user` points the MCP tool context at the same runtime
with a fresh user's **MCP token** (and an app token for REST calls).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from jolt.auth.google import GoogleIdTokenVerifier, StaticKeys
from jolt.config import Settings
from jolt.main import create_app
from jolt.mcp import context as mcp_context
from jolt.runtime import Runtime
from jolt.services.accounts import AccountService

GOOGLE_CLIENT_ID = "test-client.apps.googleusercontent.com"
GOOGLE_KID = "test-kid"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        storage_mode="memory",
        dev_auth_enabled=True,
        google_oauth_client_id=GOOGLE_CLIENT_ID,
    )


@dataclass
class GoogleSigner:
    """Mints Google-shaped RS256 ID tokens with a test key."""

    private_key: rsa.RSAPrivateKey
    kid: str = GOOGLE_KID

    def mint(self, *, key: rsa.RSAPrivateKey | None = None, headers: dict | None = None, **claims):
        now = int(time.time())
        payload = {
            "iss": "https://accounts.google.com",
            "aud": GOOGLE_CLIENT_ID,
            "sub": "1234567890",
            "email": "ada@example.com",
            "email_verified": True,
            "name": "Ada Lovelace",
            "picture": "https://example.com/ada.png",
            "iat": now,
            "exp": now + 3600,
        }
        payload.update(claims)
        payload = {k: v for k, v in payload.items() if v is not None}
        return jwt.encode(
            payload,
            key or self.private_key,
            algorithm="RS256",
            headers={"kid": self.kid, **(headers or {})},
        )


_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def google_signer() -> GoogleSigner:
    return GoogleSigner(private_key=_KEY)


@pytest.fixture
def runtime(settings: Settings, google_signer: GoogleSigner) -> Runtime:
    rt = Runtime.in_memory(settings)
    rt.google = GoogleIdTokenVerifier(
        settings.google_client_ids,
        StaticKeys({google_signer.kid: google_signer.private_key.public_key()}),
    )
    return rt


@pytest.fixture
def client(settings: Settings, runtime: Runtime):
    app = create_app(settings=settings, runtime=runtime)
    with TestClient(app) as c:
        yield c


def dev_sign_in(client: TestClient, email: str | None = None, **extra) -> dict:
    """Sign in through POST /v1/auth/dev; returns {user_id, token, user}."""
    email = email or f"learner-{uuid.uuid4().hex[:8]}@example.com"
    resp = client.post("/v1/auth/dev", json={"email": email, **extra})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return {"user_id": body["user"]["id"], "token": body["app_token"], "user": body["user"]}


@pytest.fixture
def account(client: TestClient) -> dict:
    """A fresh signed-in user: {user_id, token (app token), user}."""
    return dev_sign_in(client, display_name="Test learner")


@pytest.fixture
def auth(account: dict) -> dict:
    return {"Authorization": f"Bearer {account['token']}"}


@dataclass
class McpUser:
    user_id: str
    token: str  # MCP token: what the MCP connection carries
    app_token: str  # the same user's app token, for REST calls


@pytest.fixture
async def mcp_user(runtime: Runtime):
    """Point the MCP tools at `runtime` with a fresh user's MCP token."""
    service = AccountService(runtime)
    signed_in = await service.sign_in_dev(f"agent-{uuid.uuid4().hex[:8]}@example.com")
    issued = await service.create_mcp_token(signed_in.user.id, "Test agent")
    mcp_context.set_runtime(runtime)
    mcp_context.set_current_token(issued.token)
    yield McpUser(user_id=signed_in.user.id, token=issued.token, app_token=signed_in.app_token)
    mcp_context.set_current_token(None)
