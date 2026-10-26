"""Rate limits and request size limits (BE-10).

**Rate limits** are in-process token buckets, so they hold *per replica*
(production runs one replica while MCP uses SSE; with N replicas a client can
get up to N times the limit). Each bucket holds one minute's worth of requests
and refills continuously. Buckets:

* per token on `/v1`: `RATE_LIMIT_REST_PER_MINUTE` (requests without a token,
  e.g. sign-in, are keyed by client IP instead);
* per token on `/mcp`: `RATE_LIMIT_MCP_PER_MINUTE`, counted per **tool call**
  (in jolt/mcp/server.py) and per connection opened (`GET /mcp/sse`). A tool
  call runs inside the event stream, after its `POST /mcp/messages/` was already
  answered `202 Accepted`, so an over-limit call fails as an MCP tool error
  ("rate limit exceeded; retry in N s") instead of an HTTP 429;
* per client IP over `/v1` and `/mcp`: `RATE_LIMIT_IP_PER_MINUTE`, a coarse cap
  that also covers random-token floods.

A token is keyed by the SHA-256 of its value: the same token always maps to the
same bucket without a database read (the middleware runs before
authentication), and no token is kept in memory. Over the limit: `429` with
`Retry-After` (whole seconds) and `{"detail": "rate limit exceeded; retry in N s"}`.

**Size limits**: a request body over `REQUEST_MAX_BYTES` (`MCP_REQUEST_MAX_BYTES`
on `/mcp`) answers `413` before the app reads it. Files never go through the
API (the browser PUTs them straight to Blob with a SAS URL); the in-memory Blob
stand-in at `/_dev/blob` (memory mode only) is exempt and capped by
`UPLOAD_MAX_BYTES` instead.

`install_limits(app, settings)` adds both middlewares and documents the 429/413
responses on every `/v1` operation in the OpenAPI contract.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import parse_qs

from jolt import telemetry
from jolt.auth.context import bearer_from_header
from jolt.config import Settings

# Buckets kept per limiter. An idle bucket refills to full, so evicting the
# least recently used one never lets anyone exceed a limit.
MAX_BUCKETS = 50_000

_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


class RateLimited(Exception):
    """Over the limit; `retry_after` is in seconds."""

    def __init__(self, retry_after: float, scope: str = "token") -> None:
        self.retry_after = retry_after
        self.scope = scope
        super().__init__(f"rate limit exceeded; retry in {retry_after_header(retry_after)} s")


def retry_after_header(seconds: float) -> int:
    return max(1, math.ceil(seconds))


@dataclass
class _Bucket:
    tokens: float
    updated: float


class TokenBucketLimiter:
    """`per_minute` requests a minute per key, bursts up to `per_minute`."""

    def __init__(
        self,
        per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_buckets: int = MAX_BUCKETS,
    ) -> None:
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0  # tokens per second
        self._clock = clock
        self._max = max_buckets
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()

    def acquire(self, key: str) -> float:
        """Take one token for `key`: 0.0 if allowed, else seconds until one is free."""
        now = self._clock()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=self.capacity, updated=now)
            self._buckets[key] = bucket
            if len(self._buckets) > self._max:
                self._buckets.popitem(last=False)
        else:
            self._buckets.move_to_end(key)
            elapsed = max(0.0, now - bucket.updated)
            bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.rate)
            bucket.updated = now
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return 0.0
        return (1.0 - bucket.tokens) / self.rate


def token_key(token: str) -> str:
    return "t:" + hashlib.sha256(token.encode()).hexdigest()[:32]


class RateLimits:
    """The three limiters of one app."""

    def __init__(self, settings: Settings, clock: Callable[[], float] = time.monotonic) -> None:
        self.enabled = settings.rate_limit_enabled
        self.rest = TokenBucketLimiter(settings.rate_limit_rest_per_minute, clock=clock)
        self.mcp = TokenBucketLimiter(settings.rate_limit_mcp_per_minute, clock=clock)
        self.ip = TokenBucketLimiter(settings.rate_limit_ip_per_minute, clock=clock)

    def check_mcp_call(self, token: Optional[str]) -> None:
        """Count one MCP tool call for the connection's token; raise if over."""
        if not self.enabled or not token:
            return
        wait = self.mcp.acquire(token_key(token))
        if wait > 0:
            telemetry.record_rate_limited("mcp", "token")
            raise RateLimited(wait)


_current: Optional[RateLimits] = None


def current_limits() -> Optional[RateLimits]:
    """The limits of the most recently built app (the MCP tools read them)."""
    return _current


def check_mcp_call(token: Optional[str]) -> None:
    if _current is not None:
        _current.check_mcp_call(token)


def _client_ip(scope) -> str:
    client = scope.get("client")
    return client[0] if client else "unknown"


def _headers(scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


async def _send_json(send, status: int, detail: str, extra: list[tuple[bytes, bytes]]) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                *extra,
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class RateLimitMiddleware:
    def __init__(self, app, limits: RateLimits, settings: Settings) -> None:
        self.app = app
        self.limits = limits
        self.query_tokens = settings.mcp_query_token_enabled

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.limits.enabled or scope["method"] == "OPTIONS":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        is_rest = path.startswith("/v1/") or path == "/v1"
        is_mcp = path.startswith("/mcp")
        if not (is_rest or is_mcp):
            await self.app(scope, receive, send)
            return
        surface = "rest" if is_rest else "mcp"

        wait = self.limits.ip.acquire("ip:" + _client_ip(scope))
        scope_name = "ip"
        if wait == 0:
            headers = _headers(scope)
            token = bearer_from_header(headers.get("authorization"))
            is_stream = is_mcp and path.rstrip("/").endswith("/sse")
            if token is None and is_stream and self.query_tokens:
                values = parse_qs(scope.get("query_string", b"").decode()).get("token")
                token = values[0].strip() if values and values[0].strip() else None
            if is_rest:
                key = token_key(token) if token else "ip:" + _client_ip(scope)
                scope_name = "token" if token else "ip"
                wait = self.limits.rest.acquire(key)
            elif is_stream and token:
                # Opening a connection counts against the token's MCP budget;
                # its tool calls are counted one by one in jolt/mcp/server.py.
                scope_name = "token"
                wait = self.limits.mcp.acquire(token_key(token))
        if wait > 0:
            telemetry.record_rate_limited(surface, scope_name)
            seconds = retry_after_header(wait)
            await _send_json(
                send,
                429,
                f"rate limit exceeded; retry in {seconds} s",
                [(b"retry-after", str(seconds).encode())],
            )
            return
        await self.app(scope, receive, send)


class BodySizeLimitMiddleware:
    def __init__(self, app, settings: Settings) -> None:
        self.app = app
        self.rest_max = settings.request_max_bytes
        self.mcp_max = settings.mcp_request_max_bytes

    def _limit(self, path: str) -> Optional[int]:
        if path.startswith("/_dev/blob"):
            return None  # memory-mode Blob stand-in; capped by UPLOAD_MAX_BYTES
        return self.mcp_max if path.startswith("/mcp") else self.rest_max

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in _BODY_METHODS:
            await self.app(scope, receive, send)
            return
        limit = self._limit(scope.get("path", ""))
        if limit is None:
            await self.app(scope, receive, send)
            return
        declared = _headers(scope).get("content-length")
        if declared is not None:
            try:
                too_big = int(declared) > limit
            except ValueError:
                too_big = False  # the server rejects a malformed length itself
            if too_big:
                await _too_large(send, limit)
                return
            await self.app(scope, receive, send)
            return

        # No Content-Length (chunked): read up to the limit, then replay.
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return  # client went away
            body = message.get("body", b"")
            size += len(body)
            if size > limit:
                await _too_large(send, limit)
                return
            chunks.append(body)
            if not message.get("more_body", False):
                break
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


async def _too_large(send, limit: int) -> None:
    await _send_json(send, 413, f"request body larger than {limit} bytes", [])


# --------------------------------------------------------------------------- #
# OpenAPI
# --------------------------------------------------------------------------- #
_ERROR_REF = {"$ref": "#/components/schemas/ErrorResponse"}


def _document(app) -> None:
    """Add the 429 (every operation) and 413 (operations with a body) responses
    to the `/v1` operations of the OpenAPI document."""
    original = app.openapi

    def openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = original()
        for path, operations in schema.get("paths", {}).items():
            if not path.startswith("/v1"):
                continue
            for method, op in operations.items():
                responses = op.setdefault("responses", {})
                responses.setdefault(
                    "429",
                    {
                        "description": "Rate limit exceeded (per token, per replica); "
                        "retry after `Retry-After` seconds",
                        "headers": {
                            "Retry-After": {
                                "description": "Seconds to wait before retrying",
                                "schema": {"type": "integer"},
                            }
                        },
                        "content": {"application/json": {"schema": _ERROR_REF}},
                    },
                )
                if method.upper() in _BODY_METHODS:
                    responses.setdefault(
                        "413",
                        {
                            "description": "Request body larger than REQUEST_MAX_BYTES",
                            "content": {"application/json": {"schema": _ERROR_REF}},
                        },
                    )
        schemas = schema.setdefault("components", {}).setdefault("schemas", {})
        schemas.setdefault(
            "ErrorResponse",
            {
                "properties": {"detail": {"type": "string", "title": "Detail"}},
                "type": "object",
                "required": ["detail"],
                "title": "ErrorResponse",
            },
        )
        app.openapi_schema = schema
        return schema

    app.openapi = openapi


def install_limits(app, settings: Settings) -> RateLimits:
    """Rate and size limits on `app`. Call it before the CORS middleware is
    added, so a 429/413 still carries CORS headers."""
    global _current
    limits = RateLimits(settings)
    app.state.rate_limits = limits
    _current = limits
    # Added first = innermost: size check runs after the rate limit.
    app.add_middleware(BodySizeLimitMiddleware, settings=settings)
    app.add_middleware(RateLimitMiddleware, limits=limits, settings=settings)
    _document(app)
    return limits
