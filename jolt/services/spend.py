"""Per-user spend guard for key-backed model calls (BE-11).

Every call billed to a user's own key counts against two daily caps (UTC day):
`MODEL_DAILY_REQUEST_CAP` requests and `MODEL_DAILY_TOKEN_CAP` input + output
tokens. The count lives on the user document (`User.model_usage`), so checking
it costs nothing extra on the request path. A request is reserved (counted)
before it goes out, so two calls at once cannot both slip under the cap; the
tokens are added when the provider reports them. A cap of 0 turns the
key-backed features off. Over a cap: `429 spend_limit` with `Retry-After` set
to the next UTC midnight.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from jolt.credentials.providers import SPEND_LIMIT, ModelCallError
from jolt.domain.models import ModelUsage
from jolt.runtime import Runtime


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today(now: datetime) -> str:
    return now.date().isoformat()


def seconds_to_midnight(now: datetime) -> int:
    midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc) + timedelta(days=1)
    return max(1, int((midnight - now).total_seconds()))


@dataclass
class UsageView:
    requests_today: int
    tokens_today: int
    daily_request_cap: int
    daily_token_cap: int


class SpendGuard:
    def __init__(self, rt: Runtime) -> None:
        self._rt = rt

    @property
    def _caps(self) -> tuple[int, int]:
        s = self._rt.settings
        return s.model_daily_request_cap, s.model_daily_token_cap

    def _current(self, usage: ModelUsage | None, now: datetime) -> ModelUsage:
        if usage is None or usage.day != _today(now):
            return ModelUsage(day=_today(now))
        return usage

    def view(self, usage: ModelUsage | None) -> UsageView:
        current = self._current(usage, _now())
        request_cap, token_cap = self._caps
        return UsageView(current.requests, current.tokens, request_cap, token_cap)

    async def today(self, user_id: str) -> UsageView:
        user = await self._rt.repos.users.get_user(user_id)
        return self.view(user.model_usage if user else None)

    def _check(self, usage: ModelUsage, now: datetime) -> None:
        request_cap, token_cap = self._caps
        if usage.requests >= request_cap or usage.tokens >= token_cap:
            what = (
                "key-backed features are turned off on this server"
                if request_cap == 0 or token_cap == 0
                else f"today's limit of {request_cap} model requests or {token_cap:,} tokens "
                "on your key is reached; it resets at midnight UTC"
            )
            raise ModelCallError(SPEND_LIMIT, what, retry_after=seconds_to_midnight(now))

    async def check(self, user_id: str) -> None:
        """Refuse before any work when the caps are already reached."""
        user = await self._rt.repos.users.get_user(user_id)
        if user is not None:
            self._check(self._current(user.model_usage, _now()), _now())

    async def reserve(self, user_id: str) -> None:
        """Count one request against today's cap, or raise `spend_limit`."""
        await self._update(user_id, check=True, requests=1)

    async def record(self, user_id: str, input_tokens: int, output_tokens: int) -> UsageView:
        """Add the tokens a provider reported (never refused: the call happened)."""
        return await self._update(
            user_id, check=False, input_tokens=input_tokens, output_tokens=output_tokens
        )

    async def _update(
        self,
        user_id: str,
        *,
        check: bool,
        requests: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> UsageView:
        for _ in range(8):
            now = _now()
            user = await self._rt.repos.users.get_user(user_id)
            if user is None:
                raise ModelCallError(SPEND_LIMIT, "account not found")
            usage = self._current(user.model_usage, now)
            if check:
                self._check(usage, now)
            usage.requests += requests
            usage.input_tokens += max(0, input_tokens)
            usage.output_tokens += max(0, output_tokens)
            user.model_usage = usage
            if await self._rt.repos.users.replace(user, etag=user.etag) is not None:
                return self.view(usage)
        # Lost every race: a counting failure must not block (or bill) the user
        # twice; the next call recounts from the stored value.
        return await self.today(user_id)
