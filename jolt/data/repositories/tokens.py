"""Auth tokens repository (BE-03). Partition key = /user_id.

Listing, reading and revoking a user's tokens are single-partition. The one
exception is `find_by_lookup`, the pre-auth path that turns a bearer token into a
user: the token carries no user id, so it queries the indexed `lookup` field
across partitions. That is an equality match on an indexed field returning at
most one document, not a scan (serverless Cosmos keeps a small container on one
physical partition anyway).
"""

from __future__ import annotations

from typing import Optional

from jolt.data.repositories.base import Repository
from jolt.domain.models import AuthToken, TokenKind


class TokensRepository(Repository[AuthToken]):
    container_name = "tokens"
    model = AuthToken

    async def find_by_lookup(self, lookup: str) -> Optional[AuthToken]:
        iterator = self._c.query_items(
            query="SELECT * FROM c WHERE c.lookup = @lookup",
            parameters=[{"name": "@lookup", "value": lookup}],
        )
        async for doc in iterator:
            return self._from_doc(doc)
        return None

    async def list_kind_across_users(self, kind: TokenKind) -> list[AuthToken]:
        """Every token of `kind`, across partitions. Maintenance only (the
        expired app token sweep, BE-10): a daily fan-out, never on a request."""
        iterator = self._c.query_items(
            query="SELECT * FROM c WHERE c.kind = @kind",
            parameters=[{"name": "@kind", "value": kind.value}],
        )
        return [self._from_doc(doc) async for doc in iterator]

    async def list_for_user(self, user_id: str, kind: TokenKind) -> list[AuthToken]:
        """The user's tokens of one kind, newest first."""
        return await self._query(
            "SELECT * FROM c WHERE c.user_id = @uid AND c.kind = @kind ORDER BY c.created_at DESC",
            [{"name": "@uid", "value": user_id}, {"name": "@kind", "value": kind.value}],
            partition_value=user_id,
        )

    async def get_token(self, user_id: str, token_id: str) -> Optional[AuthToken]:
        return await self.get(token_id, partition_value=user_id)

    async def revoke(self, user_id: str, token_id: str) -> None:
        await self.delete(token_id, partition_value=user_id)
