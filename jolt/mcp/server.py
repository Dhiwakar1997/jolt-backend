"""MCP server — the product contract, mounted at /mcp (LLD §2, §3, §4).

Built on the official MCP SDK's FastMCP with HTTP+SSE transport so networked agents
and scheduled tasks can reach it. Tools are thin wrappers over the service layer;
each resolves the connection's bearer token to a user before any data access.

Auth rides the connection: an `Authorization: Bearer` header, or (for clients
whose connector settings take only a URL) `?token=` on the event-stream URL
(BE-04, docs/CONNECT.md). A small ASGI middleware copies the token into a
contextvar per request (LLD §5, §11), which the tools read via `require_user()`.
Tool calls run inside the event-stream request (the SSE transport drives the
server from it), so the token that opened the stream is the one every tool call
of that connection sees.

Every tool is wrapped so an exception it raises is also recorded on the caller's
open sync run (BE-04) before it reaches the agent as an MCP tool error.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import re
from urllib.parse import parse_qs

from mcp.server.fastmcp import FastMCP

from jolt.auth.context import bearer_from_header
from jolt.mcp.context import (
    MCPAuthError,
    authenticate_mcp,
    get_runtime,
    require_user,
    set_current_token,
)
from jolt.mcp.descriptions.tools import TOOL_DESCRIPTIONS
from jolt.mcp.tools import extract as t_extract
from jolt.mcp.tools import read as t_read
from jolt.mcp.tools import sync as t_sync
from jolt.mcp.tools import write as t_write

# (public tool name, implementation) — the MCP surface.
_TOOLS = [
    ("jolt_get_tracks", t_read.get_tracks),
    ("jolt_get_recent_concepts", t_read.get_recent_concepts),
    ("jolt_get_coverage", t_read.get_coverage),
    ("jolt_get_agenda", t_read.get_agenda),
    ("jolt_request_upload_url", t_write.request_upload_url),
    ("jolt_log_session", t_write.log_session),
    ("jolt_set_agenda", t_write.set_agenda),
    ("jolt_store_extraction", t_write.store_extraction),
    ("jolt_create_questions", t_write.create_questions),
    ("jolt_correct_concept", t_write.correct_concept),
    ("jolt_sync", t_sync.sync),
    ("jolt_get_unprocessed_sources", t_sync.get_unprocessed_sources),
    ("jolt_get_pending_gradings", t_sync.get_pending_gradings),
    ("jolt_submit_gradings", t_sync.submit_gradings),
    ("jolt_list_sources", t_extract.list_sources),
    ("jolt_get_source_content", t_extract.get_source_content),
    ("jolt_diff_extractions", t_extract.diff_extractions),
]


logger = logging.getLogger("jolt.mcp")


def _recording_errors(name: str, func):
    """Wrap a tool so an exception it raises is noted on the caller's sync run.

    The advertised signature is the tool's own (annotations resolved here, since
    FastMCP would evaluate string annotations against this module's globals).
    """

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except MCPAuthError:
            raise  # no user to record it for
        except Exception as exc:
            await _record_error(name, exc)
            raise

    wrapper.__signature__ = inspect.signature(func, eval_str=True)
    return wrapper


async def _record_error(name: str, exc: Exception) -> None:
    from jolt.services.sync_runs import SyncRunService, add_error

    try:
        user = await require_user()
        message = f"{name}: {exc}"
        await SyncRunService(get_runtime()).record(user, lambda r: add_error(r, message))
    except Exception:  # never mask the tool's own error
        logger.debug("could not record %s error on the sync run", name, exc_info=True)


def build_mcp() -> FastMCP:
    mcp = FastMCP("jolt")
    for name, func in _TOOLS:
        mcp.add_tool(
            _recording_errors(name, func), name=name, description=TOOL_DESCRIPTIONS.get(name)
        )
    return mcp


def _query_token(scope) -> str | None:
    values = parse_qs(scope.get("query_string", b"").decode()).get("token")
    return values[0].strip() if values and values[0].strip() else None


# `token=<value>` in a logged request line or URL.
_TOKEN_IN_URL = re.compile(r"([?&]token=)[^&\s\"']+")


def redact_token(text: str) -> str:
    return _TOKEN_IN_URL.sub(r"\1[redacted]", text)


class RedactTokenFilter(logging.Filter):
    """Strip `?token=` values from access-log lines (uvicorn.access)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = tuple(
                redact_token(a) if isinstance(a, str) else a
                for a in (record.args if isinstance(record.args, tuple) else (record.args,))
            )
        if isinstance(record.msg, str):
            record.msg = redact_token(record.msg)
        return True


def install_log_redaction() -> None:
    """Idempotently add the token filter to Uvicorn's access logger."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactTokenFilter) for f in access.filters):
        access.addFilter(RedactTokenFilter())


class _TokenCaptureMiddleware:
    """ASGI middleware: lift the connection's token into the MCP contextvar.

    The token is the `Authorization` header (Bearer, or a bare token), else, on
    the event stream only and when `MCP_QUERY_TOKEN_ENABLED`, the `token` query
    parameter. Opening the event stream (`GET /sse`) also checks the token up
    front, so a missing, revoked or wrong-kind token (an app token) fails the
    connection with a 401 instead of failing every tool call later. Tools still
    re-check on each call, so revoking a token cuts off an open connection at its
    next call.
    """

    def __init__(self, app) -> None:
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            token = bearer_from_header(headers.get("authorization"))
            is_stream = scope["type"] == "http" and scope.get("path", "").rstrip("/").endswith(
                "/sse"
            )
            if token is None and is_stream and _query_tokens_allowed():
                token = _query_token(scope)
            set_current_token(token)
            if is_stream:
                try:
                    await authenticate_mcp(token)
                except MCPAuthError as exc:
                    await _unauthorized(send, str(exc))
                    return
        await self._app(scope, receive, send)


def _query_tokens_allowed() -> bool:
    try:
        return get_runtime().settings.mcp_query_token_enabled
    except RuntimeError:  # pragma: no cover - runtime not initialised yet
        return False


async def _unauthorized(send, message: str) -> None:
    body = json.dumps({"detail": message}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b"Bearer"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def build_mcp_app():
    """Return the ASGI app to mount at /mcp, wrapped with token capture.

    Builds the SSE transport app directly (mcp 1.2.0 has no public `sse_app()`),
    mirroring FastMCP.run_sse_async: a `/sse` event stream plus a `/messages/`
    post endpoint. Mounted at /mcp, the client connects to /mcp/sse.
    """
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route

    mcp = build_mcp()
    server = mcp._mcp_server  # low-level Server the transport drives
    sse = SseServerTransport("/mcp/messages/")

    async def handle_sse(request):
        async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
            await server.run(streams[0], streams[1], server.create_initialization_options())

    starlette_app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ]
    )
    return _TokenCaptureMiddleware(starlette_app)
