"""FastAPI dependencies — runtime access and auth/user context (LLD §4, §5).

Every REST request resolves the bearer token → user_id here before any handler
runs. The resolved `UserContext` is what handlers pass into services, which pass
`user_id` into every repository call.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from jolt.auth.context import UserContext, bearer_from_header, resolve_token
from jolt.runtime import Runtime


def get_runtime(request: Request) -> Runtime:
    rt = getattr(request.app.state, "runtime", None)
    if rt is None:  # pragma: no cover - misconfiguration
        raise HTTPException(status_code=503, detail="runtime not initialised")
    return rt


async def get_user_context(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> UserContext:
    rt = get_runtime(request)
    token = bearer_from_header(authorization)
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
