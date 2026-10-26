"""Shared fixtures: the real app wired to in-memory Cosmos/Blob/Key Vault (A8).

`client` is a FastAPI TestClient over `create_app(...)` with an injected
in-memory `Runtime`, so REST routes run end to end with no Azure. `mcp_user`
points the MCP tool context at the same runtime and a fresh account's token, so
tool functions can be awaited directly in async tests.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jolt.config import Settings
from jolt.main import create_app
from jolt.mcp import context as mcp_context
from jolt.runtime import Runtime
from jolt.services.accounts import AccountService


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, storage_mode="memory")


@pytest.fixture
def runtime(settings: Settings) -> Runtime:
    return Runtime.in_memory(settings)


@pytest.fixture
def client(settings: Settings, runtime: Runtime):
    app = create_app(settings=settings, runtime=runtime)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def account(client: TestClient) -> dict:
    """A fresh account created through the REST API: {user_id, token}."""
    resp = client.post("/v1/accounts", json={"display_name": "Test learner"})
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
def auth(account: dict) -> dict:
    return {"Authorization": f"Bearer {account['token']}"}


@pytest.fixture
async def mcp_user(runtime: Runtime):
    """Point the MCP tools at `runtime` with a fresh account's token."""
    new = await AccountService(runtime).create_account("MCP agent")
    mcp_context.set_runtime(runtime)
    mcp_context.set_current_token(new.token)
    yield new
    mcp_context.set_current_token(None)
