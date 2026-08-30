"""Cosmos DB access — container clients and partition-key discipline (LLD §6).

This is one of the only two modules (with blob.py / vault.py) that touches Azure.
Everything above `data/` sees domain models, never Cosmos dicts.

Auth: Managed Identity (DefaultAzureCredential) is preferred per LLD §9; a key is
used only when AUTH_MODE=key (local dev / emulator).
"""

from __future__ import annotations

from dataclasses import dataclass

from azure.cosmos.aio import CosmosClient, ContainerProxy, DatabaseProxy
from azure.cosmos import PartitionKey

from jolt.config import Settings, get_settings

# Container name -> partition key path (LLD §6 partition strategy).
CONTAINER_PARTITION_KEYS: dict[str, str] = {
    "users": "/id",
    "tracks": "/user_id",  # curated tracks use /id-style point read via user_id=None bucket
    "sources": "/user_id",
    "extractions": "/source_id",
    "sessions": "/user_id",
    "concepts": "/track_id",
    "concept_states": "/user_id",
    "questions": "/concept_id",
    "reviews": "/user_id",
    "answer_feedback": "/review_id",
    "api_keys": "/user_id",
}

# Composite / range indexes worth declaring for the hot queries (LLD §6 query
# discipline). Cosmos indexes everything by default; these composite indexes make
# the ordered, filtered single-partition queries cheap.
COMPOSITE_INDEXES: dict[str, list[list[dict]]] = {
    "concept_states": [
        [{"path": "/user_id", "order": "ascending"}, {"path": "/due_at", "order": "ascending"}],
    ],
    "sources": [
        [{"path": "/processing_status", "order": "ascending"}, {"path": "/created_at", "order": "ascending"}],
    ],
    "reviews": [
        [{"path": "/status", "order": "ascending"}, {"path": "/created_at", "order": "ascending"}],
    ],
}


@dataclass
class CosmosGateway:
    client: CosmosClient
    database: DatabaseProxy
    _containers: dict[str, ContainerProxy]

    def container(self, name: str) -> ContainerProxy:
        try:
            return self._containers[name]
        except KeyError as exc:  # pragma: no cover - defensive
            raise KeyError(f"Unknown Cosmos container: {name}") from exc

    async def close(self) -> None:
        await self.client.close()


def _build_client(settings: Settings) -> CosmosClient:
    if not settings.cosmos_endpoint:
        raise RuntimeError(
            "COSMOS_ENDPOINT is not set. Fill it in .env after deploying Cosmos."
        )
    if settings.use_managed_identity:
        # Imported lazily so `key` mode has no Azure Identity dependency at import.
        from azure.identity.aio import DefaultAzureCredential

        return CosmosClient(settings.cosmos_endpoint, credential=DefaultAzureCredential())
    if not settings.cosmos_key:
        raise RuntimeError(
            "AUTH_MODE=key but COSMOS_KEY is empty. Set COSMOS_KEY in .env."
        )
    return CosmosClient(settings.cosmos_endpoint, credential=settings.cosmos_key)


async def init_cosmos(settings: Settings | None = None) -> CosmosGateway:
    """Create the client, ensure database + containers exist, return the gateway.

    Called once from the FastAPI lifespan (LLD §4 main.py assembly).
    """
    settings = settings or get_settings()
    client = _build_client(settings)
    database = await client.create_database_if_not_exists(id=settings.cosmos_database)

    containers: dict[str, ContainerProxy] = {}
    for name, pk_path in CONTAINER_PARTITION_KEYS.items():
        indexing_policy = None
        if name in COMPOSITE_INDEXES:
            indexing_policy = {
                "automatic": True,
                "indexingMode": "consistent",
                "includedPaths": [{"path": "/*"}],
                "excludedPaths": [{"path": '/"_etag"/?'}],
                "compositeIndexes": COMPOSITE_INDEXES[name],
            }
        containers[name] = await database.create_container_if_not_exists(
            id=name,
            partition_key=PartitionKey(path=pk_path),
            indexing_policy=indexing_policy,
        )
    return CosmosGateway(client=client, database=database, _containers=containers)
