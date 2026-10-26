"""End-to-end smoke test against a running Jolt backend (BE-02).

    python scripts/smoke.py --base-url https://<dev-host>
    python scripts/smoke.py --base-url http://localhost:8000      # local, see below

Walks the whole product loop over the network, the way the app and an agent
share it, with a fresh test user:

 1. GET /health (waits for a cold start; --expect-version waits for a new revision)
 2. POST /v1/accounts -> test user token
 3. MCP over SSE (/mcp/sse): initialize handshake + tools/list
 4. POST /v1/tracks
 5. POST /v1/uploads/request -> PUT a 1x1 PNG to the signed URL -> confirm
 6. The sync tool sequence over MCP with canned agent output: jolt_sync,
    jolt_get_unprocessed_sources, jolt_get_source_content (and GET the signed read
    URL back), jolt_store_extraction, jolt_log_session, jolt_create_questions
 7. GET /v1/reviews/due, POST stage1, POST stage2 (inline FSRS)
 8. jolt_get_pending_gradings + jolt_submit_gradings, then jolt_get_coverage

Exit code 0 when every step passes, 1 otherwise. It leaves the test user and its
data behind (there is no account delete yet); each run uses a new user.

Local run with no Azure (the in-memory blob fake serves the upload URL when
PUBLIC_BASE_URL is set):

    STORAGE_MODE=memory PUBLIC_BASE_URL=http://localhost:8000 \\
        uvicorn jolt.main:app --port 8000
    python scripts/smoke.py --base-url http://localhost:8000

Needs only `httpx` and `mcp` (both in requirements.txt).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.sse import sse_client

# A valid 1x1 PNG.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

EXPECTED_TOOLS = {
    "jolt_get_tracks",
    "jolt_get_recent_concepts",
    "jolt_get_coverage",
    "jolt_get_agenda",
    "jolt_request_upload_url",
    "jolt_log_session",
    "jolt_set_agenda",
    "jolt_store_extraction",
    "jolt_create_questions",
    "jolt_correct_concept",
    "jolt_sync",
    "jolt_get_unprocessed_sources",
    "jolt_get_pending_gradings",
    "jolt_submit_gradings",
    "jolt_list_sources",
    "jolt_get_source_content",
    "jolt_diff_extractions",
}

# Canned "agent" output: what a real agent would extract from the page.
CANNED_MARKDOWN = (
    "# Consistent hashing\n\n"
    "Keys and nodes are hashed onto the same ring; a key belongs to the next node "
    "clockwise. Adding a node only moves the keys between it and its predecessor."
)
CANNED_CONCEPT = "Consistent hashing places keys and nodes on one ring, so few keys move."
CORRECT_OPTION = "Only keys next to the new node move"
CANNED_QUESTION = {
    "stem": "Why does adding a node to a consistent-hashing ring move few keys?",
    "options": [CORRECT_OPTION, "Every key is rehashed", "Keys are sorted first", "Nodes vote"],
    "correct_index": 0,
    "expected_answer": "Only the keys between the new node and its predecessor change owner.",
}


class SmokeFailure(Exception):
    pass


def check(condition: Any, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


class Reporter:
    def __init__(self) -> None:
        self.passed = 0

    @contextmanager
    def step(self, name: str):
        start = time.perf_counter()
        try:
            yield
        except Exception as exc:
            ms = (time.perf_counter() - start) * 1000
            print(f"[FAIL] {name} ({ms:.0f} ms): {type(exc).__name__}: {exc}", flush=True)
            raise
        ms = (time.perf_counter() - start) * 1000
        self.passed += 1
        print(f"[ ok ] {name} ({ms:.0f} ms)", flush=True)


def _json(resp: httpx.Response, expected: int = 200) -> Any:
    if resp.status_code != expected:
        req = resp.request
        raise SmokeFailure(f"{req.method} {req.url.path} -> {resp.status_code}: {resp.text[:300]}")
    return resp.json()


async def _tool(session: ClientSession, name: str, **arguments: Any) -> dict:
    result = await session.call_tool(name, arguments)
    text = "".join(getattr(c, "text", "") for c in result.content)
    check(not result.isError, f"{name} returned an MCP error: {text[:300]}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SmokeFailure(f"{name} did not return JSON: {text[:300]}") from exc
    check(not (isinstance(data, dict) and "error" in data), f"{name}: {data}")
    return data


async def wait_for_health(
    http: httpx.AsyncClient, wait_seconds: float, expect_version: str | None
) -> dict:
    deadline = time.monotonic() + wait_seconds
    last = "no response"
    while True:
        try:
            resp = await http.get("/health")
            if resp.status_code == 200:
                body = resp.json()
                if body.get("status") == "ok" and (
                    expect_version is None or body.get("version") == expect_version
                ):
                    return body
                last = f"200 {body}"
            else:
                last = f"{resp.status_code} {resp.text[:200]}"
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
        if time.monotonic() >= deadline:
            want = f" with version {expect_version}" if expect_version else ""
            raise SmokeFailure(f"/health not ok{want} after {wait_seconds:.0f}s (last: {last})")
        await asyncio.sleep(3)


async def run(
    base_url: str,
    *,
    wait_seconds: float = 120,
    expect_version: str | None = None,
    timeout: float = 30,
) -> dict:
    """Run every step; raise on the first failure. Returns ids for the record."""
    base_url = base_url.rstrip("/")
    rep = Reporter()
    out: dict[str, Any] = {"base_url": base_url}

    async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as http:
        with rep.step("GET /health"):
            health = await wait_for_health(http, wait_seconds, expect_version)
            out["version"] = health.get("version")

        with rep.step("POST /v1/accounts (test user)"):
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            account = _json(
                await http.post("/v1/accounts", json={"display_name": f"smoke {stamp}"})
            )
            token = account["token"]
            out["user_id"] = account["user_id"]
            http.headers["Authorization"] = f"Bearer {token}"

        auth = {"Authorization": f"Bearer {token}"}
        async with sse_client(f"{base_url}/mcp/sse", headers=auth, timeout=timeout) as streams:
            async with ClientSession(*streams) as mcp:
                with rep.step("MCP initialize"):
                    init = await mcp.initialize()
                    check(init.serverInfo.name == "jolt", f"server name {init.serverInfo.name!r}")
                    out["mcp_protocol"] = init.protocolVersion

                with rep.step("MCP tools/list"):
                    names = {t.name for t in (await mcp.list_tools()).tools}
                    missing = EXPECTED_TOOLS - names
                    check(not missing, f"missing tools: {sorted(missing)}")

                with rep.step("POST /v1/tracks"):
                    track = _json(await http.post("/v1/tracks", json={"name": "Smoke test"}))
                    track_id = track["id"]

                sha = hashlib.sha256(PNG).hexdigest()
                with rep.step("POST /v1/uploads/request"):
                    ticket = _json(
                        await http.post(
                            "/v1/uploads/request",
                            json={
                                "filename": "page-1.png",
                                "content_type": "image/png",
                                "sha256": sha,
                            },
                        )
                    )
                    source_id, upload_url = ticket["source_id"], ticket["upload_url"]
                    out["source_id"] = source_id
                    check(
                        not upload_url.startswith("memory://"),
                        "upload URL is memory:// (nothing serves it): start the server with "
                        "PUBLIC_BASE_URL=<its base url> in memory mode",
                    )

                with rep.step("PUT PNG to the signed upload URL"):
                    async with httpx.AsyncClient(timeout=timeout) as raw:
                        put = await raw.put(
                            upload_url,
                            content=PNG,
                            headers={"x-ms-blob-type": "BlockBlob", "Content-Type": "image/png"},
                        )
                    check(put.status_code == 201, f"PUT -> {put.status_code}: {put.text[:300]}")

                with rep.step("POST /v1/uploads/{id}/confirm"):
                    confirmed = _json(
                        await http.post(f"/v1/uploads/{source_id}/confirm", json={"sha256": sha})
                    )
                    check(confirmed == {"status": "ready"}, f"confirm: {confirmed}")

                with rep.step("jolt_sync"):
                    plan = await _tool(mcp, "jolt_sync")
                    check(plan["unprocessed_sources"] >= 1, f"plan: {plan}")

                with rep.step("jolt_get_unprocessed_sources (claim)"):
                    claimed = await _tool(mcp, "jolt_get_unprocessed_sources")
                    ids = [s["source_id"] for s in claimed["sources"]]
                    check(source_id in ids, f"source {source_id} not claimed: {ids}")

                with rep.step("jolt_get_source_content + GET signed read URL"):
                    content = await _tool(mcp, "jolt_get_source_content", source_id=source_id)
                    async with httpx.AsyncClient(timeout=timeout) as raw:
                        got = await raw.get(content["read_url"])
                    check(got.status_code == 200, f"read URL -> {got.status_code}")
                    check(got.content == PNG, "read URL returned different bytes")

                with rep.step("jolt_store_extraction (canned)"):
                    ext = await _tool(
                        mcp,
                        "jolt_store_extraction",
                        source_id=source_id,
                        markdown=CANNED_MARKDOWN,
                        confidence=0.9,
                        model_id="smoke-canned",
                    )
                    check(ext["status"] == "active", f"extraction: {ext}")

                with rep.step("jolt_log_session (canned concept)"):
                    logged = await _tool(
                        mcp,
                        "jolt_log_session",
                        source_ids=[source_id],
                        concepts=[
                            {
                                "text": CANNED_CONCEPT,
                                "title": "Consistent hashing",
                                "track_id": track_id,
                                "concept_key": "consistent-hashing",
                                "source_id": source_id,
                            }
                        ],
                    )
                    check(len(logged["concept_ids"]) == 1, f"log_session: {logged}")
                    concept_id = logged["concept_ids"][0]

                with rep.step("jolt_create_questions (canned)"):
                    created = await _tool(
                        mcp,
                        "jolt_create_questions",
                        concept_id=concept_id,
                        questions=[CANNED_QUESTION],
                    )
                    check(len(created["question_ids"]) == 1, f"create_questions: {created}")
                    question_id = created["question_ids"][0]

                with rep.step("GET /v1/reviews/due"):
                    due = _json(await http.get("/v1/reviews/due"))
                    check(
                        any(i["question_id"] == question_id for i in due["items"]),
                        f"question {question_id} not due: {due}",
                    )

                with rep.step("POST /v1/reviews/{question_id}/stage1"):
                    s1 = _json(
                        await http.post(
                            f"/v1/reviews/{question_id}/stage1",
                            json={"concept_id": concept_id, "free_text": "Only nearby keys move"},
                        )
                    )
                    check(CORRECT_OPTION in s1["options"], f"options: {s1['options']}")
                    review_id = s1["review_id"]

                with rep.step("POST /v1/reviews/{review_id}/stage2"):
                    s2 = _json(
                        await http.post(
                            f"/v1/reviews/{review_id}/stage2",
                            json={
                                "selected_index": s1["options"].index(CORRECT_OPTION),
                                "latency_ms": 4200,
                            },
                        )
                    )
                    check(s2["correct"] is True, f"stage2: {s2}")
                    check(s2["due_at"], f"stage2 has no next due date: {s2}")
                    out["next_due_at"] = s2["due_at"]

                with rep.step("jolt_get_pending_gradings + jolt_submit_gradings"):
                    pending = await _tool(mcp, "jolt_get_pending_gradings")
                    ids = [g["review_id"] for g in pending["gradings"]]
                    check(review_id in ids, f"review {review_id} not pending: {ids}")
                    grading = [
                        {"review_id": review_id, "suggested_fsrs_grade": 3, "rationale": "smoke"}
                    ]
                    applied = await _tool(mcp, "jolt_submit_gradings", gradings=grading)
                    check(applied["applied"] == 1, f"submit_gradings: {applied}")

                with rep.step("jolt_get_coverage"):
                    cov = await _tool(mcp, "jolt_get_coverage", track_id=track_id)
                    check(cov["studied_concepts"] == 1, f"coverage: {cov}")

    out["steps_passed"] = rep.passed
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--base-url", required=True, help="e.g. https://jolt-api.<env>.azurecontainerapps.io"
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=120,
        help="seconds to wait for /health (cold start); default 120",
    )
    parser.add_argument(
        "--expect-version", help="wait until /health reports this version (e.g. the git SHA)"
    )
    parser.add_argument("--timeout", type=float, default=30, help="per-request timeout in seconds")
    args = parser.parse_args(argv)

    print(f"Jolt smoke test against {args.base_url}", flush=True)
    try:
        out = asyncio.run(
            run(
                args.base_url,
                wait_seconds=args.wait,
                expect_version=args.expect_version,
                timeout=args.timeout,
            )
        )
    except SmokeFailure as exc:
        print(f"SMOKE FAILED: {exc}", flush=True)
        return 1
    except Exception as exc:  # network errors, MCP transport errors
        print(f"SMOKE FAILED: {type(exc).__name__}: {exc}", flush=True)
        return 1
    print(f"SMOKE PASSED: {out['steps_passed']} steps", flush=True)
    print(json.dumps(out, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
