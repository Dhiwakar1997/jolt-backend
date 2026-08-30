"""Scope-enforcement test (LLD §5): a repository refuses to query without a
partition value. This is the unit test the LLD promises — a repository method that
forgets the user_id filter fails here.
"""

import pytest

from jolt.data.repositories.base import PartitionKeyRequired
from jolt.data.repositories.concepts import ConceptStatesRepository
from jolt.data.repositories.reviews import ReviewsRepository
from jolt.data.repositories.sources import SourcesRepository


class _DummyGateway:
    """Stands in for CosmosGateway; the partition check fires before it is touched."""

    def container(self, name):  # pragma: no cover - should never be reached
        raise AssertionError("container accessed despite missing partition value")


@pytest.mark.asyncio
async def test_concept_states_due_requires_user():
    repo = ConceptStatesRepository(_DummyGateway())
    with pytest.raises(PartitionKeyRequired):
        await repo._query("SELECT * FROM c", [], partition_value=None)


@pytest.mark.asyncio
async def test_sources_get_requires_partition():
    repo = SourcesRepository(_DummyGateway())
    with pytest.raises(PartitionKeyRequired):
        await repo.get("some-id", partition_value=None)


@pytest.mark.asyncio
async def test_reviews_delete_requires_partition():
    repo = ReviewsRepository(_DummyGateway())
    with pytest.raises(PartitionKeyRequired):
        await repo.delete("some-id", partition_value=None)
