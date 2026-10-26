"""Rate limits per token and request size limits (BE-10, jolt/api/limits.py)."""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from jolt.api.limits import TokenBucketLimiter
from jolt.config import Settings
from jolt.main import create_app
from jolt.mcp import context as mcp_context
from jolt.mcp import server as mcp_server
from jolt.runtime import Runtime
from jolt.services.accounts import AccountService
from tests.conftest import dev_sign_in

APP = "http://localhost:5173"


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, storage_mode="memory", dev_auth_enabled=True, **overrides)


def _client(**overrides) -> TestClient:
    settings = _settings(**overrides)
    return TestClient(create_app(settings=settings, runtime=Runtime.in_memory(settings)))


def _auth(account: dict) -> dict:
    return {"Authorization": f"Bearer {account['token']}"}


# --------------------------------------------------------------------------- #
# The bucket
# --------------------------------------------------------------------------- #
def test_token_bucket_allows_a_minute_then_refills():
    now = [1000.0]
    limiter = TokenBucketLimiter(3, clock=lambda: now[0])
    assert [limiter.acquire("a") for _ in range(3)] == [0.0, 0.0, 0.0]
    wait = limiter.acquire("a")
    assert wait == pytest.approx(20.0)  # 3/min -> one token every 20 s
    assert limiter.acquire("b") == 0.0  # keys are independent
    now[0] += 20.0
    assert limiter.acquire("a") == 0.0
    assert limiter.acquire("a") > 0


def test_token_bucket_memory_is_bounded():
    limiter = TokenBucketLimiter(1, clock=lambda: 0.0, max_buckets=2)
    for key in ("a", "b", "c"):
        limiter.acquire(key)
    assert len(limiter._buckets) == 2


# --------------------------------------------------------------------------- #
# /v1
# --------------------------------------------------------------------------- #
def test_rest_rate_limit_is_per_token_with_retry_after():
    with _client(rate_limit_rest_per_minute=3) as c:
        ada = dev_sign_in(c)  # sign-in has no token: keyed by client IP
        bob = dev_sign_in(c)
        for _ in range(3):
            assert c.get("/v1/me", headers=_auth(ada)).status_code == 200
        over = c.get("/v1/me", headers={**_auth(ada), "Origin": APP})
        assert over.status_code == 429
        assert int(over.headers["retry-after"]) >= 1
        assert over.json()["detail"].startswith("rate limit exceeded; retry in ")
        # CORS stays outermost, so the browser can read the 429 and Retry-After.
        assert over.headers["access-control-allow-origin"] == APP
        assert "retry-after" in over.headers["access-control-expose-headers"].lower()
        # Another token has its own bucket.
        assert c.get("/v1/me", headers=_auth(bob)).status_code == 200


def test_rest_rate_limit_without_a_token_is_per_ip():
    with _client(rate_limit_rest_per_minute=2) as c:
        assert c.get("/v1/me").status_code == 401
        assert c.get("/v1/me").status_code == 401
        assert c.get("/v1/me").status_code == 429


def test_ip_cap_covers_everything_and_health_is_exempt():
    with _client(rate_limit_ip_per_minute=2) as c:
        assert c.get("/v1/me").status_code == 401
        assert c.get("/mcp/sse").status_code == 401
        assert c.get("/v1/me").status_code == 429
        assert c.get("/health").status_code == 200  # probes are never limited


def test_preflight_is_not_counted():
    with _client(rate_limit_rest_per_minute=1, cors_origins=APP) as c:
        for _ in range(3):
            pre = c.options(
                "/v1/me", headers={"Origin": APP, "Access-Control-Request-Method": "GET"}
            )
            assert pre.status_code == 200
        assert c.get("/v1/me").status_code == 401  # the one real request


def test_rate_limits_can_be_switched_off():
    with _client(rate_limit_enabled=False, rate_limit_rest_per_minute=1) as c:
        statuses = {c.get("/v1/me").status_code for _ in range(5)}
        assert statuses == {401}


# --------------------------------------------------------------------------- #
# /mcp
# --------------------------------------------------------------------------- #
def test_opening_mcp_connections_counts_against_the_token():
    # A bogus token: the bucket is counted before auth, so the first open gets
    # the auth answer (401) and the second the rate-limit one.
    with _client(rate_limit_mcp_per_minute=1) as c:
        assert c.get("/mcp/sse", headers={"Authorization": "Bearer jolt_live_x"}).status_code == 401
        over = c.get("/mcp/sse?token=jolt_live_x")
        assert over.status_code == 429
        assert int(over.headers["retry-after"]) >= 1


async def test_mcp_tool_calls_are_limited_per_token():
    settings = _settings(rate_limit_mcp_per_minute=2)
    runtime = Runtime.in_memory(settings)
    create_app(settings=settings, runtime=runtime)  # installs this app's limits
    service = AccountService(runtime)
    tokens = []
    for _ in range(2):
        user = await service.sign_in_dev(f"agent-{uuid.uuid4().hex[:8]}@example.com")
        tokens.append((await service.create_mcp_token(user.user.id, "Agent")).token)
    mcp_context.set_runtime(runtime)
    mcp = mcp_server.build_mcp()
    try:
        mcp_context.set_current_token(tokens[0])
        for _ in range(2):
            content = await mcp.call_tool("jolt_get_tracks", {})
            assert isinstance(json.loads(content[0].text), (list, dict))
        with pytest.raises(Exception, match="rate limit exceeded; retry in"):
            await mcp.call_tool("jolt_get_tracks", {})
        # A second token is unaffected.
        mcp_context.set_current_token(tokens[1])
        await mcp.call_tool("jolt_get_tracks", {})
    finally:
        mcp_context.set_current_token(None)


# --------------------------------------------------------------------------- #
# Body size
# --------------------------------------------------------------------------- #
def test_oversized_rest_body_is_413_before_the_handler():
    with _client(request_max_bytes=2048) as c:
        ada = dev_sign_in(c)
        big = {"name": "x" * 4096}
        resp = c.post("/v1/tracks", json=big, headers=_auth(ada))
        assert resp.status_code == 413
        assert resp.json() == {"detail": "request body larger than 2048 bytes"}
        # Under the cap the request goes through.
        assert c.post("/v1/tracks", json={"name": "Physics"}, headers=_auth(ada)).status_code in (
            200,
            201,
        )


def test_oversized_chunked_body_is_413():
    with _client(request_max_bytes=2048) as c:
        ada = dev_sign_in(c)

        def chunks():
            for _ in range(5):
                yield b"x" * 1000

        resp = c.post(
            "/v1/tracks",
            content=chunks(),
            headers={**_auth(ada), "Content-Type": "application/json"},
        )
        assert resp.status_code == 413


def test_small_chunked_body_is_replayed_to_the_handler():
    with _client(request_max_bytes=2048) as c:
        ada = dev_sign_in(c)

        def chunks():
            yield b'{"name": '
            yield b'"Chunked"}'

        resp = c.post(
            "/v1/tracks",
            content=chunks(),
            headers={**_auth(ada), "Content-Type": "application/json"},
        )
        assert resp.status_code in (200, 201), resp.text
        assert resp.json()["name"] == "Chunked"


def test_mcp_has_its_own_body_cap():
    with _client(request_max_bytes=1024, mcp_request_max_bytes=4096) as c:
        resp = c.post("/mcp/messages/?session_id=x", content=b"x" * 2048)
        assert resp.status_code != 413  # under the MCP cap (the transport answers)
        resp = c.post("/mcp/messages/?session_id=x", content=b"x" * 8192)
        assert resp.status_code == 413


def test_dev_blob_uploads_are_exempt():
    with _client(request_max_bytes=1024, public_base_url="http://testserver") as c:
        resp = c.put("/_dev/blob/sources/u/s/0-a.png?sp=cw&sig=bad", content=b"x" * 4096)
        assert resp.status_code != 413  # the stand-in checks its own signature


def test_openapi_documents_429_and_413():
    with _client() as c:
        spec = c.get("/openapi.json").json()
    me = spec["paths"]["/v1/me"]["get"]["responses"]
    assert "429" in me and "Retry-After" in me["429"]["headers"]
    assert "413" not in me
    tracks = spec["paths"]["/v1/tracks"]["post"]["responses"]
    assert {"413", "429"} <= set(tracks)
