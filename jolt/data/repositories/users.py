"""Users repository. Partition key = /id (point reads by user)."""

from __future__ import annotations

from typing import Optional

from jolt.data.repositories.base import Repository
from jolt.domain.models import User


class UsersRepository(Repository[User]):
    container_name = "users"
    model = User

    async def get_user(self, user_id: str) -> Optional[User]:
        return await self.get(user_id, partition_value=user_id)

    async def find_by_token_hash_candidates(self) -> list[User]:
        """Token verification needs the candidate set of hashes.

        Argon2 hashes are not reversible and the salt differs per hash, so we
        cannot look a token up by hash directly. The token itself carries no
        user id (opaque), so resolution walks candidates. For the expected
        low-thousands user count in MVP this is acceptable; if it grows, add a
        fast non-secret lookup key (e.g. a truncated HMAC index) — noted for
        later, not built now.
        """
        # Cross-partition read is unavoidable here and is the one place a query
        # runs without a user_id — it is the pre-auth path that establishes it.
        items: list[User] = []
        iterator = self._c.query_items(query="SELECT * FROM c")
        async for doc in iterator:
            items.append(self._from_doc(doc))
        return items
