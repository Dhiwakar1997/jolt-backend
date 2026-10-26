"""Cosmos DB access — container clients and partition-key discipline (LLD §6).

This is one of the only two modules (with blob.py / vault.py) that touches Azure.
Everything above `data/` sees domain models, never Cosmos dicts.

Auth: Managed Identity (DefaultAzureCredential) is preferred per LLD §9; a key is
used only when AUTH_MODE=key (local dev / emulator).
"""

from __future__ import annotations

from dataclasses import dataclass

from azure.cosmos import PartitionKey
from azure.cosmos.aio import ContainerProxy, CosmosClient, DatabaseProxy

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
    "review_sessions": "/user_id",  # BE-06: review session queues
    "answer_feedback": "/review_id",
    "api_keys": "/user_id",
    "tokens": "/user_id",  # app + MCP tokens (BE-03); resolved by the indexed `lookup`
}

# Composite / range indexes worth declaring for the hot queries (LLD §6 query
# discipline). Cosmos indexes everything by default; these composite indexes make
# the ordered, filtered single-partition queries cheap.
COMPOSITE_INDEXES: dict[str, list[list[dict]]] = {
    "concept_states": [
        [{"path": "/user_id", "order": "ascending"}, {"path": "/due_at", "order": "ascending"}],
    ],
    "sources": [
        [
            {"path": "/processing_status", "order": "ascending"},
            {"path": "/created_at", "order": "ascending"},
        ],
    ],
    "reviews": [
        [{"path": "/status", "order": "ascending"}, {"path": "/created_at", "order": "ascending"}],
    ],
}


def indexing_policy(container: str) -> dict | None:
    """The container's indexing policy, or None for the Cosmos default."""
    if container not in COMPOSITE_INDEXES:
        return None
    return {
        "automatic": True,
        "indexingMode": "consistent",
        "includedPaths": [{"path": "/*"}],
        "excludedPaths": [{"path": '/"_etag"/?'}],
        "compositeIndexes": COMPOSITE_INDEXES[container],
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
        raise RuntimeError("COSMOS_ENDPOINT is not set. Fill it in .env after deploying Cosmos.")
    if settings.use_managed_identity:
        # Imported lazily so `key` mode has no Azure Identity dependency at import.
        from azure.identity.aio import DefaultAzureCredential

        return CosmosClient(settings.cosmos_endpoint, credential=DefaultAzureCredential())
    if not settings.cosmos_key:
        raise RuntimeError("AUTH_MODE=key but COSMOS_KEY is empty. Set COSMOS_KEY in .env.")
    return CosmosClient(settings.cosmos_endpoint, credential=settings.cosmos_key)


async def init_cosmos(settings: Settings | None = None) -> CosmosGateway:
    """Create the client and return the gateway with one client per container.

    Called once from the FastAPI lifespan (LLD §4 main.py assembly).

    With a key (local dev / emulator) the database and containers are created if
    missing. Under Managed Identity they are not: Cosmos data-plane RBAC (the
    "Built-in Data Contributor" role the app holds) cannot create databases or
    containers, so in Azure they are provisioned by `infra/` (Bicep) from
    `infra/cosmos-containers.json`, which a test keeps in step with
    `CONTAINER_PARTITION_KEYS` / `COMPOSITE_INDEXES` above.
    """
    settings = settings or get_settings()
    client = _build_client(settings)

    if settings.use_managed_identity:
        database = client.get_database_client(settings.cosmos_database)
        containers = {
            name: database.get_container_client(name) for name in CONTAINER_PARTITION_KEYS
        }
        return CosmosGateway(client=client, database=database, _containers=containers)

    database = await client.create_database_if_not_exists(id=settings.cosmos_database)
    containers: dict[str, ContainerProxy] = {}
    for name, pk_path in CONTAINER_PARTITION_KEYS.items():
        containers[name] = await database.create_container_if_not_exists(
            id=name,
            partition_key=PartitionKey(path=pk_path),
            indexing_policy=indexing_policy(name),
        )
    return CosmosGateway(client=client, database=database, _containers=containers)
