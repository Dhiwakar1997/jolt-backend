"""The in-memory Cosmos fake keeps the guarantees the code relies on (A8)."""

from __future__ import annotations

import pytest
from azure.cosmos import exceptions as cosmos_exc

from jolt.data.memory import InMemoryContainer, InMemoryCosmosGateway
from jolt.data.repositories.sources import SourcesRepository
from jolt.domain.models import Source


def _source(user_id: str = "u1", **kw) -> Source:
    fields = {"filename": "a.png", "content_type": "image/png", "sha256": "h", "blob_path": "p"}
    return Source(user_id=user_id, **{**fields, **kw})


async def test_replace_with_stale_etag_returns_none():
    repo = SourcesRepository(InMemoryCosmosGateway())
    created = await repo.create(_source())
    first = created.model_copy(deep=True)
    second = created.model_copy(deep=True)
    assert await repo.replace(first, etag=first.etag) is not None
    # `second` still carries the old etag: it lost the race.
    assert await repo.replace(second, etag=second.etag) is None


async def test_string_match_condition_is_rejected_like_the_sdk():
    c = InMemoryContainer("sources", "/user_id")
    doc = await c.create_item({"id": "s1", "user_id": "u1"})
    with pytest.raises(TypeError):
        await c.replace_item("s1", doc, etag=doc["_etag"], match_condition="IfMatch")


async def test_create_twice_conflicts_and_missing_read_is_not_found():
    c = InMemoryContainer("sources", "/user_id")
    await c.create_item({"id": "s1", "user_id": "u1"})
    with pytest.raises(cosmos_exc.CosmosResourceExistsError):
        await c.create_item({"id": "s1", "user_id": "u1"})
    # Same id in another partition is a different document.
    await c.create_item({"id": "s1", "user_id": "u2"})
    with pytest.raises(cosmos_exc.CosmosResourceNotFoundError):
        await c.read_item("s1", partition_key="u3")


async def test_queries_stay_inside_the_partition_and_order_and_limit():
    repo = SourcesRepository(InMemoryCosmosGateway())
    for i in range(3):
        await repo.create(_source("u1", filename=f"{i}.png"))
    await repo.create(_source("u2"))
    rows = await repo.list_for_user("u1")
    assert [r.filename for r in rows] == ["2.png", "1.png", "0.png"]  # created_at DESC
    # Only confirmed uploads are offered to a sync (BE-05).
    assert await repo.list_unprocessed("u1", limit=2) == []
    for row in rows:
        row.confirmed_at = row.created_at
        await repo.replace(row)
    assert len(await repo.list_unprocessed("u1", limit=2)) == 2


async def test_unsupported_query_fails_loudly():
    c = InMemoryContainer("sources", "/user_id")
    with pytest.raises(NotImplementedError):
        c.query_items("SELECT VALUE COUNT(1) FROM c", [], partition_key="u1")
