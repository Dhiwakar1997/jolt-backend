"""MCP request context — per-connection token resolution (LLD §5, §11).

The per-user token rides the MCP connection as an `Authorization: Bearer` header
(the exact mechanics are an open question in §11; header is the default and is what
Claude Desktop / Codex send). An ASGI middleware on the mounted MCP app copies that
header into a contextvar for the duration of each request, and tools resolve it to
a `user_id` before touching any data — the same scope enforcement as the REST side.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

from jolt.auth.context import UserContext, resolve_token
from jolt.runtime import Runtime

_current_token: ContextVar[Optional[str]] = ContextVar("jolt_mcp_token", default=None)
_runtime: Optional[Runtime] = None


def set_runtime(rt: Runtime) -> None:
    global _runtime
    _runtime = rt


def get_runtime() -> Runtime:
    if _runtime is None:  # pragma: no cover - misconfiguration
        raise RuntimeError("MCP runtime not initialised")
    return _runtime


def set_current_token(token: Optional[str]) -> None:
    _current_token.set(token)


class MCPAuthError(Exception):
    pass


async def require_user() -> UserContext:
    """Resolve the current connection's token → UserContext, or raise."""
    token = _current_token.get()
    if not token:
        raise MCPAuthError("missing bearer token on MCP connection")
    ctx = await resolve_token(token, get_runtime().repos.users)
    if ctx is None:
        raise MCPAuthError("invalid token")
    return ctx
