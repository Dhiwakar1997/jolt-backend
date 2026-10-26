"""In-memory stand-ins for Cosmos, Blob and Key Vault (decision A8).

These sit behind the same seams as the Azure gateways (`CosmosGateway`,
`BlobGateway`, `VaultClient`), so repositories, services, REST routes and MCP
tools run unchanged on top of them. They exist for two reasons:

* API-level tests without Azure (`tests/conftest.py` builds the app on them).
* A local server with no cloud resources: `STORAGE_MODE=memory`.

Fidelity goals, in order:

1. Same partition discipline as Cosmos: every document lives in exactly one
   partition (read from the container's partition-key path), point reads and
   deletes need the partition value, and a query without one fans out over all
   partitions (only `tokens.find_by_lookup`, the pre-auth path, does that).
2. Same optimistic concurrency: every write mints a new `_etag`; a replace with
   `match_condition=MatchConditions.IfNotModified` and a stale etag raises
   `CosmosAccessConditionFailedError`. A match condition that is not a
   `MatchConditions` member raises `TypeError`, exactly like the real SDK.
3. Same errors: `CosmosResourceNotFoundError` and `CosmosResourceExistsError`.
4. The query dialect is only the subset the repositories use (see
   `_parse_query`). Anything else raises `NotImplementedError`, so a new query
   shape fails loudly in tests instead of silently returning wrong rows.
   Comparisons are on the stored JSON values, so ISO timestamps compare as
   strings, as they do in Cosmos.

Nothing here is used when `STORAGE_MODE=azure` (the default).
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import itertools
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional
from urllib.parse import quote

from azure.core import MatchConditions
from azure.cosmos import exceptions as cosmos_exc

from jolt.config import Settings, get_settings
from jolt.credentials.vault import secret_name
from jolt.data.blob import page_blob_path
from jolt.data.cosmos import CONTAINER_PARTITION_KEYS

# --------------------------------------------------------------------------- #
# Cosmos
# --------------------------------------------------------------------------- #
_SELECT_RE = re.compile(
    r"^\s*SELECT\s+\*\s+FROM\s+c"
    r"(?:\s+WHERE\s+(?P<where>.+?))?"
    r"(?:\s+ORDER\s+BY\s+c\.(?P<order_field>\w+)(?:\s+(?P<order_dir>ASC|DESC))?)?"
    r"(?:\s+OFFSET\s+(?P<offset>@?\w+)\s+LIMIT\s+(?P<limit>@?\w+))?"
    r"\s*$",
    re.IGNORECASE | re.DOTALL,
)
_COND_RE = re.compile(
    r"^\s*c\.(?P<field>\w+)\s*(?P<op><=|>=|!=|<>|=|<|>)\s*(?P<param>@\w+)\s*$",
    re.IGNORECASE,
)
_IN_RE = re.compile(r"^\s*c\.(?P<field>\w+)\s+IN\s*\((?P<params>[^)]*)\)\s*$", re.IGNORECASE)

_OPS = {
    "=": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<>": lambda a, b: a != b,
    "<": lambda a, b: a is not None and b is not None and a < b,
    "<=": lambda a, b: a is not None and b is not None and a <= b,
    ">": lambda a, b: a is not None and b is not None and a > b,
    ">=": lambda a, b: a is not None and b is not None and a >= b,
}


@dataclass
class _Query:
    predicates: list  # callables doc -> bool
    order_field: Optional[str]
    descending: bool
    offset: int
    limit: Optional[int]


def _resolve(token: str, params: dict[str, Any]) -> Any:
    if token.startswith("@"):
        if token not in params:
            raise ValueError(f"query parameter {token} not supplied")
        return params[token]
    return int(token)


def _parse_query(query: str, parameters: Optional[list[dict]]) -> _Query:
    params = {p["name"]: p["value"] for p in (parameters or [])}
    m = _SELECT_RE.match(query)
    if not m:
        raise NotImplementedError(f"in-memory Cosmos cannot run query: {query!r}")

    predicates = []
    where = m.group("where")
    if where:
        for clause in re.split(r"\s+AND\s+", where, flags=re.IGNORECASE):
            cm = _COND_RE.match(clause)
            if cm:
                fname, op = cm.group("field"), cm.group("op")
                value = _resolve(cm.group("param"), params)
                predicates.append(
                    lambda d, f=fname, o=_OPS[op], v=value: o(d.get(f), v)  # noqa: E731
                )
                continue
            im = _IN_RE.match(clause)
            if im:
                fname = im.group("field")
                values = [_resolve(t.strip(), params) for t in im.group("params").split(",")]
                predicates.append(lambda d, f=fname, vs=values: d.get(f) in vs)
                continue
            raise NotImplementedError(f"in-memory Cosmos cannot evaluate clause: {clause!r}")

    offset = _resolve(m.group("offset"), params) if m.group("offset") else 0
    limit = _resolve(m.group("limit"), params) if m.group("limit") else None
    return _Query(
        predicates=predicates,
        order_field=m.group("order_field"),
        descending=(m.group("order_dir") or "ASC").upper() == "DESC",
        offset=offset,
        limit=limit,
    )


async def _aiter(items: list[dict]) -> AsyncIterator[dict]:
    for item in items:
        yield item


class InMemoryContainer:
    """Mimics the subset of `azure.cosmos.aio.ContainerProxy` the repos use."""

    def __init__(self, name: str, partition_key_path: str) -> None:
        self.id = name
        self._pk_field = partition_key_path.lstrip("/")
        # partition value -> item id -> stored document
        self._items: dict[Any, dict[str, dict]] = {}
        self._etags = itertools.count(1)

    # -- helpers -------------------------------------------------------------
    def _pk_of(self, body: dict) -> Any:
        return body.get(self._pk_field)

    def _stamp(self, body: dict) -> dict:
        doc = copy.deepcopy(body)
        doc["_etag"] = f'"{self.id}-{next(self._etags)}"'
        return doc

    def _find(self, item_id: str, partition_key: Any) -> Optional[dict]:
        return self._items.get(partition_key, {}).get(item_id)

    @staticmethod
    def _not_found(item_id: str) -> cosmos_exc.CosmosResourceNotFoundError:
        return cosmos_exc.CosmosResourceNotFoundError(
            status_code=404, message=f"Entity with the specified id does not exist: {item_id}"
        )

    def all_items(self) -> list[dict]:
        """Every stored document (test helper; not part of the Cosmos API)."""
        return [copy.deepcopy(d) for part in self._items.values() for d in part.values()]

    # -- writes --------------------------------------------------------------
    async def create_item(self, body: dict, **_: Any) -> dict:
        pk = self._pk_of(body)
        if self._find(body["id"], pk) is not None:
            raise cosmos_exc.CosmosResourceExistsError(
                status_code=409,
                message=f"Entity with the specified id already exists: {body['id']}",
            )
        doc = self._stamp(body)
        self._items.setdefault(pk, {})[body["id"]] = doc
        return copy.deepcopy(doc)

    async def upsert_item(self, body: dict, **_: Any) -> dict:
        doc = self._stamp(body)
        self._items.setdefault(self._pk_of(body), {})[body["id"]] = doc
        return copy.deepcopy(doc)

    async def replace_item(
        self,
        item: str,
        body: dict,
        *,
        etag: Optional[str] = None,
        match_condition: Optional[MatchConditions] = None,
        **_: Any,
    ) -> dict:
        if match_condition is not None and not isinstance(match_condition, MatchConditions):
            # Same guard as azure.cosmos._base._get_match_headers.
            raise TypeError(f"Invalid match condition: {match_condition}")
        if match_condition is None and etag is not None:
            raise ValueError("'etag' specified without 'match_condition'.")
        pk = self._pk_of(body)
        current = self._find(item, pk)
        if current is None:
            raise self._not_found(item)
        if match_condition == MatchConditions.IfNotModified and current.get("_etag") != etag:
            raise cosmos_exc.CosmosAccessConditionFailedError(
                status_code=412,
                message="Operation cannot be performed because one of the "
                "specified precondition is not met.",
            )
        doc = self._stamp({**body, "id": item})
        self._items[pk][item] = doc
        return copy.deepcopy(doc)

    async def delete_item(self, item: str, partition_key: Any, **_: Any) -> None:
        if self._find(item, partition_key) is None:
            raise self._not_found(item)
        del self._items[partition_key][item]

    # -- reads ---------------------------------------------------------------
    async def read_item(self, item: str, partition_key: Any, **_: Any) -> dict:
        doc = self._find(item, partition_key)
        if doc is None:
            raise self._not_found(item)
        return copy.deepcopy(doc)

    def query_items(
        self,
        query: str,
        parameters: Optional[list[dict]] = None,
        partition_key: Any = None,
        **_: Any,
    ) -> AsyncIterator[dict]:
        q = _parse_query(query, parameters)
        if partition_key is None:
            pool = [d for part in self._items.values() for d in part.values()]
        else:
            pool = list(self._items.get(partition_key, {}).values())
        rows = [d for d in pool if all(p(d) for p in q.predicates)]
        if q.order_field:
            # Cosmos orders undefined/null first ascending; mirror that.
            rows.sort(
                key=lambda d: (d.get(q.order_field) is not None, d.get(q.order_field) or ""),
                reverse=q.descending,
            )
        rows = rows[q.offset :]
        if q.limit is not None:
            rows = rows[: q.limit]
        return _aiter([copy.deepcopy(d) for d in rows])


class InMemoryCosmosGateway:
    """Drop-in for `jolt.data.cosmos.CosmosGateway`."""

    def __init__(self) -> None:
        self._containers = {
            name: InMemoryContainer(name, pk) for name, pk in CONTAINER_PARTITION_KEYS.items()
        }

    def container(self, name: str) -> InMemoryContainer:
        try:
            return self._containers[name]
        except KeyError as exc:  # pragma: no cover - defensive
            raise KeyError(f"Unknown Cosmos container: {name}") from exc

    async def close(self) -> None:
        return None


# --------------------------------------------------------------------------- #
# Blob
# --------------------------------------------------------------------------- #
@dataclass
class _StoredBlob:
    data: bytes
    content_type: Optional[str]
    content_disposition: Optional[str] = None


@dataclass
class InMemoryBlobGateway:
    """Drop-in for `jolt.data.blob.BlobGateway`.

    By default URLs are opaque `memory://` strings that nothing serves; tests
    simulate the client's direct PUT with `put()`. With a `base_url` of
    `<server>/_dev/blob` (set from `PUBLIC_BASE_URL` in memory mode), the URLs are
    served by the dev-only routes in `jolt/api/routes/dev_blob.py`, so a browser
    or `scripts/smoke.py` can PUT and GET for real. The `sig` is an HMAC over
    permission + path with `signing_key`, checked by `verify()`; like a SAS, a
    URL grants exactly one permission on exactly one blob.
    """

    container: str = "sources"
    base_url: str = "memory://blob"
    signing_key: str = "memory"
    _blobs: dict[str, _StoredBlob] = field(default_factory=dict)

    def blob_path(self, user_id: str, source_id: str, filename: str, page: int = 0) -> str:
        return page_blob_path(user_id, source_id, filename, page)

    def _sig(self, blob_path: str, perm: str) -> str:
        msg = f"{perm}:{self.container}/{blob_path}".encode()
        return hmac.new(self.signing_key.encode(), msg, hashlib.sha256).hexdigest()[:32]

    def _url(self, blob_path: str, perm: str) -> str:
        sig = self._sig(blob_path, perm)
        path = quote(blob_path)
        return f"{self.base_url}/{self.container}/{path}?sp={perm}&sig={sig}"

    def verify(self, blob_path: str, perm: str, sig: str, needed: str) -> bool:
        """True if (`perm`, `sig`) was minted for `blob_path` and grants `needed`."""
        if not perm or not set(needed) & set(perm):
            return False
        return hmac.compare_digest(sig, self._sig(blob_path, perm))

    async def upload_url(self, blob_path: str) -> str:
        return self._url(blob_path, "cw")

    async def read_url(
        self,
        blob_path: str,
        ttl_seconds: Optional[int] = None,
        content_type: Optional[str] = None,
    ) -> str:
        return self._url(blob_path, "r")  # memory URLs do not expire

    async def upload_bytes(
        self,
        blob_path: str,
        data: bytes,
        content_type: str,
        content_disposition: Optional[str] = None,
    ) -> None:
        self.put(blob_path, data, content_type, content_disposition)

    async def list_paths(self, prefix: str) -> list[str]:
        return sorted(p for p in self._blobs if p.startswith(prefix))

    async def delete_prefix(self, prefix: str) -> int:
        names = await self.list_paths(prefix)
        for name in names:
            self._blobs.pop(name, None)
        return len(names)

    def paths(self) -> list[str]:
        """Every stored blob path (test helper)."""
        return sorted(self._blobs)

    async def stat(self, blob_path: str) -> Optional[dict]:
        blob = self._blobs.get(blob_path)
        if blob is None:
            return None
        return {"size": len(blob.data), "content_type": blob.content_type}

    async def sha256(self, blob_path: str) -> Optional[str]:
        blob = self._blobs.get(blob_path)
        return hashlib.sha256(blob.data).hexdigest() if blob is not None else None

    async def delete(self, blob_path: str) -> None:
        self._blobs.pop(blob_path, None)

    def put(
        self,
        blob_path: str,
        data: bytes,
        content_type: Optional[str] = None,
        content_disposition: Optional[str] = None,
    ) -> None:
        """Store bytes as the client's direct PUT to the upload URL would."""
        self._blobs[blob_path] = _StoredBlob(
            data=data, content_type=content_type, content_disposition=content_disposition
        )

    def get(self, blob_path: str) -> Optional[_StoredBlob]:
        return self._blobs.get(blob_path)

    async def close(self) -> None:
        return None


# --------------------------------------------------------------------------- #
# Key Vault
# --------------------------------------------------------------------------- #
class _SecretNotFound(Exception):
    pass


@dataclass
class InMemoryVault:
    """Drop-in for `jolt.credentials.vault.VaultClient` (same secret naming)."""

    _secrets: dict[str, str] = field(default_factory=dict)

    async def set_key(self, user_id: str, provider: str, plaintext: str) -> str:
        name = secret_name(user_id, provider)
        self._secrets[name] = plaintext
        return name

    async def get_key(self, user_id: str, provider: str) -> str:
        try:
            return self._secrets[secret_name(user_id, provider)]
        except KeyError as exc:
            raise _SecretNotFound(secret_name(user_id, provider)) from exc

    async def delete_key(self, user_id: str, provider: str) -> None:
        self._secrets.pop(secret_name(user_id, provider), None)

    def names(self) -> list[str]:
        """Stored secret names (test helper)."""
        return sorted(self._secrets)

    async def close(self) -> None:
        return None


def build_memory_gateways(settings: Settings | None = None):
    """Return (cosmos, blob, vault) fakes for `Runtime`."""
    settings = settings or get_settings()
    return (
        InMemoryCosmosGateway(),
        InMemoryBlobGateway(
            container=settings.blob_container,
            base_url=(
                f"{settings.public_base_url.rstrip('/')}/_dev/blob"
                if settings.public_base_url
                else "memory://blob"
            ),
            signing_key=settings.token_hash_pepper,
        ),
        (InMemoryVault()),
    )
