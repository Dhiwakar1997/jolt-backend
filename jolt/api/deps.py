"""FastAPI dependencies — runtime access and auth/user context (LLD §4, §5).

Every authenticated REST request resolves its bearer token → user_id here before
any handler runs. `/v1` accepts **app tokens** only (BE-03); an MCP token gets a
401. The resolved `UserContext` is what handlers pass into services, which pass
`user_id` into every repository call.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from jolt.auth.context import AuthError, UserContext, authenticate, bearer_from_header
from jolt.config import Settings
from jolt.domain.models import TokenKind
from jolt.runtime import Runtime


def get_runtime(request: Request) -> Runtime:
    rt = getattr(request.app.state, "runtime", None)
    if rt is None:  # pragma: no cover - misconfiguration
        raise HTTPException(status_code=503, detail="runtime not initialised")
    return rt


# Declares the `HTTPBearer` security scheme in the OpenAPI contract. It never
# rejects on its own (auto_error=False): the raw header is still parsed by
# `bearer_from_header`, which also tolerates a bare token.
_bearer_scheme = HTTPBearer(
    auto_error=False,
    description="App token (`jolt_app_…`) from sign-in: `Bearer <token>`",
)


async def get_user_context(
    request: Request,
    _credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)] = None,
) -> UserContext:
    rt = get_runtime(request)
    try:
        return await authenticate(
            bearer_from_header(request.headers.get("authorization")),
            kind=TokenKind.APP,
            tokens=rt.repos.tokens,
            users=rt.repos.users,
            pepper=rt.settings.token_hash_pepper,
            idle_timeout=app_token_idle_timeout(rt.settings),
        )
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def app_token_idle_timeout(settings: Settings) -> Optional[timedelta]:
    """`APP_TOKEN_IDLE_DAYS` as a timedelta; None when app tokens never expire."""
    days = settings.app_token_idle_days
    return timedelta(days=days) if days > 0 else None


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]
UserDep = Annotated[UserContext, Depends(get_user_context)]
