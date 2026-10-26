"""Users repository. Partition key = /id (point reads by user).

User ids are derived from the sign-in identity (`user_id_for`), so finding a user
by their Google `sub` is a point read, never a query. Token resolution lives in
`TokensRepository` (BE-03). The one scan is `list_all`, used only by the daily
maintenance sweep (BE-10), never on a request path.
"""

from __future__ import annotations

from typing import Optional

from jolt.data.repositories.base import Repository
from jolt.domain.models import User


class UsersRepository(Repository[User]):
    container_name = "users"
    model = User

    async def get_user(self, user_id: str) -> Optional[User]:
        return await self.get(user_id, partition_value=user_id)

    async def list_all(self) -> list[User]:
        """Every user, across partitions. Maintenance only (jolt.maintenance):
        a daily fan-out over a small container, never on a request path."""
        iterator = self._c.query_items(query="SELECT * FROM c", parameters=[])
        return [self._from_doc(doc) async for doc in iterator]
