"""MCP server — the product contract, mounted at /mcp (LLD §2, §3, §4).

Built on the official MCP SDK's FastMCP with HTTP+SSE transport so networked agents
and scheduled tasks can reach it. Tools are thin wrappers over the service layer;
each resolves the connection's bearer token to a user before any data access.

Auth rides the connection as an `Authorization` header. A small ASGI middleware
copies that header into a contextvar per request (LLD §5, §11), which the tools
read via `require_user()`.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from jolt.auth.context import bearer_from_header
from jolt.mcp.context import set_current_token
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


def build_mcp() -> FastMCP:
    mcp = FastMCP("jolt")
    for name, func in _TOOLS:
        mcp.add_tool(func, name=name, description=TOOL_DESCRIPTIONS.get(name))
    return mcp


class _TokenCaptureMiddleware:
    """ASGI middleware: lift the Authorization header into the MCP contextvar."""

    def __init__(self, app) -> None:
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            set_current_token(bearer_from_header(headers.get("authorization")))
        await self._app(scope, receive, send)


def build_mcp_app():
    """Return the ASGI app to mount at /mcp, wrapped with token capture.

    Builds the SSE transport app directly (mcp 1.2.0 has no public `sse_app()`),
    mirroring FastMCP.run_sse_async: a `/sse` event stream plus a `/messages/`
    post endpoint. Mounted at /mcp, the client connects to /mcp/sse.
    """
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route
    from mcp.server.sse import SseServerTransport

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
