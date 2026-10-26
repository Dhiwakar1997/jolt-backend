"""init_cosmos never calls control-plane creates under Managed Identity (BE-02).

Cosmos data-plane RBAC cannot create databases or containers; in Azure they come
from infra/ (Bicep). With a key (emulator) they are still created on startup.
"""

from __future__ import annotations

from jolt.config import Settings
from jolt.data import cosmos
from jolt.data.cosmos import CONTAINER_PARTITION_KEYS


class _Database:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    def get_container_client(self, name):
        self.calls.append(("get_container", name))
        return f"container:{name}"

    async def create_container_if_not_exists(self, id, partition_key, indexing_policy=None):
        self.calls.append(("create_container", id, indexing_policy is not None))
        return f"container:{id}"


class _Client:
    def __init__(self) -> None:
        self.calls: list = []

    def get_database_client(self, name):
        self.calls.append(("get_database", name))
        return _Database(self.calls)

    async def create_database_if_not_exists(self, id):
        self.calls.append(("create_database", id))
        return _Database(self.calls)


def _settings(auth_mode: str) -> Settings:
    return Settings(
        _env_file=None, auth_mode=auth_mode, cosmos_endpoint="https://x", cosmos_key="k"
    )


async def test_managed_identity_only_gets_clients(monkeypatch):
    client = _Client()
    monkeypatch.setattr(cosmos, "_build_client", lambda s: client)
    gw = await cosmos.init_cosmos(_settings("managed_identity"))
    assert not [c for c in client.calls if c[0].startswith("create")]
    assert gw.container("concept_states") == "container:concept_states"
    assert {c[1] for c in client.calls if c[0] == "get_container"} == set(CONTAINER_PARTITION_KEYS)


async def test_key_mode_creates_with_composite_indexes(monkeypatch):
    client = _Client()
    monkeypatch.setattr(cosmos, "_build_client", lambda s: client)
    await cosmos.init_cosmos(_settings("key"))
    created = {c[1]: c[2] for c in client.calls if c[0] == "create_container"}
    assert set(created) == set(CONTAINER_PARTITION_KEYS)
    assert created["concept_states"] and created["sources"] and created["reviews"]
    assert not created["users"]
