"""Repository base — partition-key discipline and domain-model mapping (LLD §6).

Every repository returns domain models (Pydantic), never raw Cosmos dicts, so
`domain/` never sees Azure types. Every query requires its partition value up
front; a repository that tries to query without one raises, which is what the
scope-enforcement unit test asserts (LLD §5).
"""

from __future__ import annotations

from typing import Generic, Optional, Type, TypeVar

from azure.cosmos import exceptions as cosmos_exc

from jolt.data.cosmos import CosmosGateway
from jolt.domain.models import DocBase

T = TypeVar("T", bound=DocBase)


class PartitionKeyRequired(RuntimeError):
    """Raised when a query is attempted without a partition value."""


class Repository(Generic[T]):
    container_name: str
    model: Type[T]

    def __init__(self, gateway: CosmosGateway) -> None:
        self._gw = gateway

    @property
    def _c(self):
        return self._gw.container(self.container_name)

    # -- serialization -------------------------------------------------------
    def _to_doc(self, model: T) -> dict:
        doc = model.model_dump(mode="json", by_alias=False, exclude_none=False)
        doc.pop("etag", None)  # Cosmos manages _etag
        return doc

    def _from_doc(self, doc: dict) -> T:
        return self.model.model_validate(doc)

    # -- writes --------------------------------------------------------------
    async def create(self, model: T) -> T:
        doc = await self._c.create_item(self._to_doc(model))
        return self._from_doc(doc)

    async def upsert(self, model: T) -> T:
        doc = await self._c.upsert_item(self._to_doc(model))
        return self._from_doc(doc)

    async def replace(self, model: T, *, etag: Optional[str] = None) -> Optional[T]:
        """Replace with optional optimistic concurrency (LLD §8).

        Returns None on an etag mismatch so callers can detect a lost race
        (used by lease acquisition).
        """
        kwargs: dict = {}
        etag = etag or model.etag
        if etag:
            kwargs["match_condition"] = "IfMatch"
            kwargs["etag"] = etag
        try:
            doc = await self._c.replace_item(item=model.id, body=self._to_doc(model), **kwargs)
        except cosmos_exc.CosmosAccessConditionFailedError:
            return None
        return self._from_doc(doc)

    # -- reads ---------------------------------------------------------------
    async def get(self, item_id: str, partition_value: str) -> Optional[T]:
        if partition_value is None:
            raise PartitionKeyRequired(f"{self.container_name}.get needs a partition value")
        try:
            doc = await self._c.read_item(item=item_id, partition_key=partition_value)
        except cosmos_exc.CosmosResourceNotFoundError:
            return None
        return self._from_doc(doc)

    async def _query(
        self,
        query: str,
        params: list[dict],
        *,
        partition_value: str,
    ) -> list[T]:
        if partition_value is None:
            raise PartitionKeyRequired(
                f"{self.container_name} query needs a partition value (scope enforcement)"
            )
        items: list[T] = []
        iterator = self._c.query_items(
            query=query,
            parameters=params,
            partition_key=partition_value,
        )
        async for doc in iterator:
            items.append(self._from_doc(doc))
        return items

    async def delete(self, item_id: str, partition_value: str) -> None:
        if partition_value is None:
            raise PartitionKeyRequired(f"{self.container_name}.delete needs a partition value")
        try:
            await self._c.delete_item(item=item_id, partition_key=partition_value)
        except cosmos_exc.CosmosResourceNotFoundError:
            pass
