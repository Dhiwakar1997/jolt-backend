"""FastAPI dependencies — runtime access and auth/user context (LLD §4, §5).

Every REST request resolves the bearer token → user_id here before any handler
runs. The resolved `UserContext` is what handlers pass into services, which pass
`user_id` into every repository call.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from jolt.auth.context import UserContext, bearer_from_header, resolve_token
from jolt.runtime import Runtime


def get_runtime(request: Request) -> Runtime:
    rt = getattr(request.app.state, "runtime", None)
    if rt is None:  # pragma: no cover - misconfiguration
        raise HTTPException(status_code=503, detail="runtime not initialised")
    return rt


# Declares the `HTTPBearer` security scheme in the OpenAPI contract. It never
# rejects on its own (auto_error=False): the raw header is still parsed by
# `bearer_from_header`, which also tolerates a bare token.
_bearer_scheme = HTTPBearer(auto_error=False, description="App token: `Bearer <token>`")


async def get_user_context(
    request: Request,
    _credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)] = None,
) -> UserContext:
    rt = get_runtime(request)
    token = bearer_from_header(request.headers.get("authorization"))
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    ctx = await resolve_token(token, rt.repos.users)
    if ctx is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return ctx


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]
UserDep = Annotated[UserContext, Depends(get_user_context)]
