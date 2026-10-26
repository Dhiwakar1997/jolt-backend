"""End-to-end smoke test against a running Jolt backend (BE-02 to BE-07, BE-09).

    python scripts/smoke.py --base-url https://<dev-host>
    python scripts/smoke.py --base-url http://localhost:8000      # local, see below

Walks the whole product loop over the network, the way the app and an agent
share it, with a fresh test user:

 1. GET /health (waits for a cold start; --expect-version waits for a new revision)
 2. POST /v1/auth/dev -> test user + app token (the dev sign-in; Google sign-in
    needs a real Google account, so the smoke test cannot use it), GET /v1/me
 3. POST /v1/mcp-tokens -> MCP token; /mcp refuses the app token
 4. MCP over SSE (/mcp/sse) with the MCP token: initialize handshake + tools/list
 5. POST /v1/tracks
 6. POST /v1/uploads/request for a two-page note (one source, two files) ->
    PUT each page to its signed URL -> confirm (size and hash per page) ->
    GET /v1/sources shows it unprocessed
 7. The sync tool sequence over MCP with canned agent output: jolt_sync,
    jolt_get_unprocessed_sources, jolt_get_source_content (and GET every page's
    signed read URL back), jolt_store_extraction, jolt_log_session,
    jolt_create_questions; then GET /v1/sources/{id} (originals, active read, past
    reads), POST /v1/sources/{id}/reread and jolt_sync lists it in reread_sources,
    claim it again (supersede=true) and store the new read, GET /v1/storage
 8. The review loop (BE-06): GET /v1/reviews/due; PATCH /v1/settings to allow
    question rewrites; POST /v1/review-sessions -> stage 1 and stage 2 (each sent
    twice with the same client_request_id: same answer back) -> POST
    /v1/review-sessions/{id}/end with "what moved"
 9. jolt_get_pending_gradings + jolt_submit_gradings with a rewritten question ->
    version 2 (GET /v1/concepts/{id}/questions), the next session serves it, the
    review shows `graded` with feedback; POST /v1/concepts/{id}/unretire; then
    jolt_get_coverage
10. Tracks and concepts (BE-07): POST /v1/tracks with an outline (draft agenda),
    PATCH rename and archive, GET /v1/tracks counts, GET /v1/tracks/{id} (items,
    due today, recent sources), GET /v1/concepts/{id} (timeline, versions,
    adaptive note), POST /v1/concepts/{id}/correction -> jolt_sync counts it ->
    jolt_get_pending_corrections -> jolt_correct_concept clears it; read again once
    more, answered with jolt_store_extraction(supersede, concepts) ->
    reconciliation (unchanged + new) and jolt_continue_reconciliation
11. jolt_sync again: done, which closes the sync run; GET /v1/sync/status and
    /v1/sync/runs show it (client label, counts, the question_rewritten change and
    the concept the reconciliation added), GET /v1/sync/prompt
12. Curated DSA track (BE-09): GET /v1/tracks/catalog, POST /v1/tracks/dsa/enrol,
    GET /v1/tracks/dsa/library and /decision-tree, POST /v1/review-sessions
    {kind: dsa_patterns, time_budget_minutes} -> one pattern_match question: stage
    1 (no title anywhere before the answer) -> stage 2 (match, question_title).
    Skipped, with a note, when the catalog has no DSA track yet (import it with
    scripts/import_dsa.py); --require-dsa makes that a failure, --skip-dsa skips
13. MCP with the token in the URL (?token=, how a URL-only connector connects)
14. GET /v1/me shows the onboarding progress (incl. first_sync_at); revoke the
    MCP token (/mcp then refuses it); sign out (/v1 then refuses the app token)

Exit code 0 when every step passes, 1 otherwise. It leaves the test user and its
data behind (there is no account delete yet); each run uses a new user.

The server must have DEV_AUTH_ENABLED=true. A server with real storage also has a
DEV_AUTH_SECRET: pass it with --dev-auth-secret or the JOLT_DEV_AUTH_SECRET
environment variable (the deploy workflow reads it from Key Vault).

Local run with no Azure (the in-memory blob fake serves the upload URL when
PUBLIC_BASE_URL is set):

    STORAGE_MODE=memory DEV_AUTH_ENABLED=true PUBLIC_BASE_URL=http://localhost:8000 \\
        DSA_SEED_FROM=../Jolt uvicorn jolt.main:app --port 8000
    python scripts/smoke.py --base-url http://localhost:8000

Needs only `httpx` and `mcp` (both in requirements.txt).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import secrets
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
    "jolt_get_pending_corrections",
    "jolt_continue_reconciliation",
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


async def _sse_status(base_url: str, token: str, timeout: float) -> int:
    """Status of opening the MCP event stream with `token` (closes at once)."""
    async with httpx.AsyncClient(timeout=timeout) as raw:
        headers = {"Authorization": f"Bearer {token}"}
        async with raw.stream("GET", f"{base_url}/mcp/sse", headers=headers) as resp:
            return resp.status_code


async def run(
    base_url: str,
    *,
    wait_seconds: float = 120,
    expect_version: str | None = None,
    timeout: float = 30,
    dev_auth_secret: str | None = None,
    skip_dsa: bool = False,
    require_dsa: bool = False,
) -> dict:
    """Run every step; raise on the first failure. Returns ids for the record."""
    base_url = base_url.rstrip("/")
    rep = Reporter()
    out: dict[str, Any] = {"base_url": base_url}

    async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as http:
        with rep.step("GET /health"):
            health = await wait_for_health(http, wait_seconds, expect_version)
            out["version"] = health.get("version")

        with rep.step("POST /v1/auth/dev (test user)"):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            email = f"smoke-{stamp.lower()}-{secrets.token_hex(3)}@smoke.jolt.test"
            headers = {"X-Jolt-Dev-Auth-Secret": dev_auth_secret} if dev_auth_secret else {}
            signed_in = _json(
                await http.post(
                    "/v1/auth/dev",
                    json={"email": email, "display_name": f"smoke {stamp}"},
                    headers=headers,
                )
            )
            app_token = signed_in["app_token"]
            check(app_token.startswith("jolt_app_"), "app token has the wrong prefix")
            out["user_id"] = signed_in["user"]["id"]
            http.headers["Authorization"] = f"Bearer {app_token}"

        with rep.step("GET /v1/me (new user)"):
            me = _json(await http.get("/v1/me"))
            check(me["user"]["email"] == email, f"me: {me}")
            check(not me["onboarding"]["has_mcp_token"], f"onboarding: {me['onboarding']}")
            check(me["mcp_server_url"].endswith("/mcp/sse"), f"mcp_server_url: {me}")
            out["mcp_server_url"] = me["mcp_server_url"]

        with rep.step("POST /v1/mcp-tokens"):
            created = _json(
                await http.post("/v1/mcp-tokens", json={"label": "smoke test"}), expected=201
            )
            mcp_token, mcp_token_id = created["token"], created["id"]
            check(mcp_token.startswith("jolt_live_"), "MCP token has the wrong prefix")

        with rep.step("/mcp refuses the app token"):
            status = await _sse_status(base_url, app_token, timeout)
            check(status == 401, f"GET /mcp/sse with the app token -> {status}")

        auth = {"Authorization": f"Bearer {mcp_token}"}
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
                # A two-page note: one source, two files (decision A7).
                pages = [
                    {
                        "filename": f"page-{i}.png",
                        "content_type": "image/png",
                        "size": len(PNG),
                        "sha256": sha,
                    }
                    for i in (1, 2)
                ]
                with rep.step("POST /v1/uploads/request (two pages)"):
                    ticket = _json(
                        await http.post(
                            "/v1/uploads/request",
                            json={"track_id": track_id, "title": "Smoke note", "pages": pages},
                        )
                    )
                    source_id, upload_url = ticket["source_id"], ticket["upload_url"]
                    out["source_id"] = source_id
                    check(len(ticket["pages"]) == 2, f"page URLs: {ticket['pages']}")
                    check(
                        not upload_url.startswith("memory://"),
                        "upload URL is memory:// (nothing serves it): start the server with "
                        "PUBLIC_BASE_URL=<its base url> in memory mode",
                    )

                with rep.step("PUT each page to its signed upload URL"):
                    async with httpx.AsyncClient(timeout=timeout) as raw:
                        for target in ticket["pages"]:
                            put = await raw.put(
                                target["upload_url"],
                                content=PNG,
                                headers={
                                    "x-ms-blob-type": "BlockBlob",
                                    "Content-Type": "image/png",
                                },
                            )
                            check(
                                put.status_code == 201,
                                f"PUT page {target['index']} -> {put.status_code}: "
                                f"{put.text[:300]}",
                            )

                with rep.step("POST /v1/uploads/{id}/confirm"):
                    checks = [{"index": i, "sha256": sha, "size": len(PNG)} for i in (0, 1)]
                    confirmed = _json(
                        await http.post(
                            f"/v1/uploads/{source_id}/confirm",
                            json={"sha256": sha, "pages": checks},
                        )
                    )
                    check(confirmed == {"status": "ready"}, f"confirm: {confirmed}")

                with rep.step("GET /v1/sources (unprocessed)"):
                    listed = _json(await http.get("/v1/sources", params={"track_id": track_id}))
                    row = next((s for s in listed if s["id"] == source_id), None)
                    check(row is not None, f"source {source_id} not listed: {listed}")
                    check(row["processing_status"] == "unprocessed", f"source: {row}")
                    check(row["page_count"] == 2, f"source: {row}")

                with rep.step("jolt_sync (opens a sync run, returns the plan)"):
                    plan = await _tool(mcp, "jolt_sync")
                    check(plan["unprocessed_sources"] >= 1, f"plan: {plan}")
                    check(not plan["done"], f"plan says done with work waiting: {plan}")
                    tools = [st["tool"] for st in plan["steps"]]
                    check(
                        tools[0] == "jolt_get_unprocessed_sources" and tools[-1] == "jolt_sync",
                        f"plan steps: {tools}",
                    )
                    run_id = plan["run_id"]

                with rep.step("jolt_get_unprocessed_sources (claim)"):
                    claimed = await _tool(mcp, "jolt_get_unprocessed_sources")
                    ids = [s["source_id"] for s in claimed["sources"]]
                    check(source_id in ids, f"source {source_id} not claimed: {ids}")

                with rep.step("jolt_get_source_content + GET every signed read URL"):
                    content = await _tool(mcp, "jolt_get_source_content", source_id=source_id)
                    check(content["page_count"] == 2, f"content: {content}")
                    async with httpx.AsyncClient(timeout=timeout) as raw:
                        for page in content["pages"]:
                            got = await raw.get(page["read_url"])
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

                with rep.step("GET /v1/sources/{id} (originals, active read, past reads)"):
                    detail = _json(await http.get(f"/v1/sources/{source_id}"))
                    check(detail["processing_status"] == "processed", f"status: {detail}")
                    check(len(detail["pages"]) == 2, f"pages: {detail['pages']}")
                    active = detail["active_read"] or {}
                    check(active.get("markdown") == CANNED_MARKDOWN, f"active read: {active}")
                    check(
                        (active.get("concepts_found"), active.get("questions_written")) == (1, 1),
                        f"active read counts: {active}",
                    )
                    check(len(detail["reads"]) == 1, f"reads: {detail['reads']}")

                with rep.step("POST /v1/sources/{id}/reread + jolt_sync lists it"):
                    _json(
                        await http.post(
                            f"/v1/sources/{source_id}/reread", json={"preferred_model": "claude"}
                        )
                    )
                    plan = await _tool(mcp, "jolt_sync")
                    rereads = {r["source_id"]: r["preferred_model"] for r in plan["reread_sources"]}
                    check(rereads.get(source_id) == "claude", f"plan: {plan}")
                    check(plan["rereads"] == 1 and not plan["done"], f"plan: {plan}")
                    check(plan["run_id"] == run_id, f"re-read opened another run: {plan}")

                with rep.step("Read again: claim (supersede) + jolt_store_extraction"):
                    claimed = await _tool(mcp, "jolt_get_unprocessed_sources")
                    by_id = {s["source_id"]: s for s in claimed["sources"]}
                    item = by_id.get(source_id)
                    check(item is not None, f"re-read source not claimed: {claimed}")
                    check(item["supersede"] and item["preferred_model"] == "claude", f"{item}")
                    ext = await _tool(
                        mcp,
                        "jolt_store_extraction",
                        source_id=source_id,
                        markdown=CANNED_MARKDOWN,
                        confidence=0.95,
                        model_id="smoke-canned",
                        supersede=True,
                        concepts=[{"text": CANNED_CONCEPT, "title": "Consistent hashing"}],
                    )
                    check(ext["status"] == "active", f"extraction: {ext}")
                    rec = ext["reconciliation"] or {}
                    changes = [d["change"] for d in rec.get("deltas", [])]
                    check(changes == ["unchanged"], f"reconciliation: {rec}")
                    detail = _json(await http.get(f"/v1/sources/{source_id}"))
                    check(detail["processing_status"] == "processed", f"status: {detail}")
                    check(detail["reread_requested"] is None, f"re-read still pending: {detail}")
                    check(len(detail["reads"]) == 2, f"reads: {detail['reads']}")

                with rep.step("GET /v1/storage"):
                    storage = _json(await http.get("/v1/storage"))
                    check(storage["bytes_used"] == 2 * len(PNG), f"storage: {storage}")
                    check(storage["page_count"] == 2, f"storage: {storage}")

                with rep.step("GET /v1/reviews/due"):
                    due = _json(await http.get("/v1/reviews/due"))
                    check(
                        any(i["question_id"] == question_id for i in due["items"]),
                        f"question {question_id} not due: {due}",
                    )

                with rep.step("PATCH /v1/settings (allow question rewrites)"):
                    prefs = _json(
                        await http.patch("/v1/settings", json={"allow_question_rewrite": True})
                    )
                    check(prefs["allow_question_rewrite"] is True, f"settings: {prefs}")

                with rep.step("POST /v1/review-sessions"):
                    session = _json(await http.post("/v1/review-sessions", json={}))
                    session_id = session["id"]
                    item = next(
                        (i for i in session["items"] if i["question_id"] == question_id), None
                    )
                    check(item is not None, f"question {question_id} not queued: {session}")
                    check("options" not in item, "the queue leaked the options before stage 1")
                    check(item["version"] == 1, f"queue item: {item}")

                with rep.step("POST /v1/reviews/{question_id}/stage1 (idempotent)"):
                    s1_body = {
                        "concept_id": concept_id,
                        "free_text": "Only nearby keys move",
                        "client_request_id": f"smoke-s1-{secrets.token_hex(4)}",
                        "session_id": session_id,
                    }
                    s1 = _json(await http.post(f"/v1/reviews/{question_id}/stage1", json=s1_body))
                    check(CORRECT_OPTION in s1["options"], f"options: {s1['options']}")
                    review_id = s1["review_id"]
                    s1_again = _json(
                        await http.post(f"/v1/reviews/{question_id}/stage1", json=s1_body)
                    )
                    check(s1_again == s1, f"stage 1 retry differs: {s1_again} vs {s1}")

                with rep.step("POST /v1/reviews/{review_id}/stage2 (idempotent)"):
                    s2_body = {
                        "selected_index": s1["options"].index(CORRECT_OPTION),
                        "latency_ms": 4200,
                        "client_request_id": f"smoke-s2-{secrets.token_hex(4)}",
                    }
                    s2 = _json(await http.post(f"/v1/reviews/{review_id}/stage2", json=s2_body))
                    check(s2["correct"] is True, f"stage2: {s2}")
                    check(s2["due_at"], f"stage2 has no next due date: {s2}")
                    check(s2["grading"] == "provisional", f"stage2: {s2}")
                    check(s2["provisional_grade"] in (1, 2, 3, 4), f"stage2: {s2}")
                    out["next_due_at"] = s2["due_at"]
                    s2_again = _json(
                        await http.post(f"/v1/reviews/{review_id}/stage2", json=s2_body)
                    )
                    check(s2_again == s2, f"stage 2 retry differs: {s2_again} vs {s2}")

                with rep.step("POST /v1/review-sessions/{id}/end (what moved)"):
                    summary = _json(await http.post(f"/v1/review-sessions/{session_id}/end"))
                    check(summary["reviewed"] == 1 and summary["correct"] == 1, f"{summary}")
                    moved = {m["concept_id"]: m for m in summary["moved"]}
                    check(concept_id in moved, f"concept did not move: {summary['moved']}")
                    check(moved[concept_id]["new_due_at"], f"moved: {moved[concept_id]}")
                    check(len(summary["upcoming"]) == 7, f"upcoming: {summary['upcoming']}")

                with rep.step("jolt_get_pending_gradings + jolt_submit_gradings (rewrite)"):
                    pending = await _tool(mcp, "jolt_get_pending_gradings")
                    ids = [g["review_id"] for g in pending["gradings"]]
                    check(review_id in ids, f"review {review_id} not pending: {ids}")
                    check(pending["allow_question_rewrite"] is True, f"pending: {pending}")
                    grading = [
                        {
                            "review_id": review_id,
                            "suggested_fsrs_grade": 3,
                            "rationale": "smoke: right idea, name the predecessor too",
                            "rewritten_question": {
                                **CANNED_QUESTION,
                                "stem": "Which keys change owner when a node joins the ring?",
                                "rewrite_reason": "smoke: probe the detail",
                            },
                        }
                    ]
                    applied = await _tool(mcp, "jolt_submit_gradings", gradings=grading)
                    check(applied["applied"] == 1, f"submit_gradings: {applied}")
                    check(len(applied["rewritten"]) == 1, f"submit_gradings: {applied}")
                    version2 = applied["rewritten"][0]
                    check(version2["version"] == 2, f"rewritten: {version2}")
                    check(version2["previous_version_id"] == question_id, f"{version2}")

                with rep.step("GET /v1/concepts/{id}/questions (version chain)"):
                    chain = _json(await http.get(f"/v1/concepts/{concept_id}/questions"))
                    versions = [(q["version"], q["active"]) for q in chain["question_versions"]]
                    check(versions == [(1, False), (2, True)], f"versions: {versions}")
                    current = chain["current_question"] or {}
                    check(current.get("question_id") == version2["question_id"], f"{chain}")

                with rep.step("GET /v1/reviews/{id} (graded, feedback)"):
                    review = _json(await http.get(f"/v1/reviews/{review_id}"))
                    check(review["grading"] == "graded", f"review: {review}")
                    check(review["feedback"], f"review has no feedback: {review}")

                with rep.step("POST /v1/review-sessions (serves version 2)"):
                    nxt = _json(
                        await http.post("/v1/review-sessions", json={"concept_id": concept_id})
                    )
                    items = [(i["question_id"], i["version"]) for i in nxt["items"]]
                    check(items == [(version2["question_id"], 2)], f"queue: {items}")
                    _json(await http.post(f"/v1/review-sessions/{nxt['id']}/end"))

                with rep.step("POST /v1/concepts/{id}/unretire"):
                    un = _json(await http.post(f"/v1/concepts/{concept_id}/unretire"))
                    check(un["retired"] is False, f"unretire: {un}")

                with rep.step("jolt_get_coverage"):
                    cov = await _tool(mcp, "jolt_get_coverage", track_id=track_id)
                    check(cov["studied_concepts"] == 1, f"coverage: {cov}")

                # ---- BE-07: tracks and concept read models --------------------
                with rep.step("POST /v1/tracks (outline -> draft) + PATCH rename, archive"):
                    drafted = _json(
                        await http.post(
                            "/v1/tracks",
                            json={
                                "name": "Smoke outline",
                                "kind": "notes",
                                "outline": "Replication\n  Quorums\nConsistency",
                            },
                        )
                    )
                    agenda = drafted["agenda"]
                    check(agenda["status"] == "draft", f"outline agenda: {agenda}")
                    check(len(agenda["syllabus"]) == 3 and agenda["outline"], f"{agenda}")
                    renamed = _json(
                        await http.patch(
                            f"/v1/tracks/{drafted['id']}", json={"name": "Smoke outline 2"}
                        )
                    )
                    check(renamed["name"] == "Smoke outline 2", f"rename: {renamed}")
                    archived = _json(
                        await http.patch(f"/v1/tracks/{drafted['id']}", json={"archived": True})
                    )
                    check(archived["archived"] is True, f"archive: {archived}")

                with rep.step("GET /v1/tracks (counts)"):
                    rows = {t["id"]: t for t in _json(await http.get("/v1/tracks"))}
                    row = rows[track_id]
                    counts = (row["concept_count"], row["source_count"])
                    check(counts == (1, 1), f"track counts: {row}")
                    check(rows[drafted["id"]]["archived"] is True, "archived flag missing")

                with rep.step("GET /v1/tracks/{id} (Track detail)"):
                    td = _json(await http.get(f"/v1/tracks/{track_id}"))
                    item = next(i for i in td["items"] if i["concept_id"] == concept_id)
                    check(item["question_count"] == 1, f"item: {item}")
                    check(item["question_version"] == 2, f"item: {item}")
                    check(item["found_in"]["source_id"] == source_id, f"item: {item}")
                    check([s["id"] for s in td["recent_sources"]] == [source_id], f"{td}")

                with rep.step("GET /v1/concepts/{id} (Concept)"):
                    cd = _json(await http.get(f"/v1/concepts/{concept_id}"))
                    check(cd["summary"] == CANNED_CONCEPT, f"summary: {cd['summary']}")
                    check(len(cd["timeline"]) >= 1 and cd["timeline"][0]["free_text"], f"{cd}")
                    check(cd["current_question"]["version"] == 2, f"current: {cd}")
                    check(cd["adaptive_note"], f"no adaptive note: {cd}")
                    check(cd["found_in"]["source_id"] == source_id, f"found_in: {cd}")

                with rep.step("POST /v1/concepts/{id}/correction -> jolt_correct_concept"):
                    queued = _json(
                        await http.post(
                            f"/v1/concepts/{concept_id}/correction",
                            json={"note": "Say 'clockwise' explicitly."},
                        )
                    )
                    check(queued["status"] == "queued", f"correction: {queued}")
                    plan = await _tool(mcp, "jolt_sync")
                    check(plan["pending_corrections"] == 1, f"plan: {plan}")
                    pending = await _tool(mcp, "jolt_get_pending_corrections")
                    check(
                        [c["concept_id"] for c in pending["corrections"]] == [concept_id],
                        f"pending: {pending}",
                    )
                    fixed = await _tool(
                        mcp,
                        "jolt_correct_concept",
                        track_id=track_id,
                        concept_id=concept_id,
                        text=CANNED_CONCEPT,
                    )
                    check(fixed["corrections_cleared"] == 1, f"correct: {fixed}")

                with rep.step("Read again + jolt_store_extraction(supersede, concepts)"):
                    _json(await http.post(f"/v1/sources/{source_id}/reread", json={}))
                    claimed = await _tool(mcp, "jolt_get_unprocessed_sources", limit=10)
                    check(
                        any(
                            s["source_id"] == source_id and s["supersede"]
                            for s in claimed["sources"]
                        ),
                        f"read-again not offered: {claimed}",
                    )
                    ext2 = await _tool(
                        mcp,
                        "jolt_store_extraction",
                        source_id=source_id,
                        markdown=CANNED_MARKDOWN + "\n\nVirtual nodes even out the load.",
                        confidence=0.9,
                        model_id="smoke-canned",
                        supersede=True,
                        concepts=[
                            {"text": CANNED_CONCEPT, "title": "Consistent hashing"},
                            {
                                "text": "Virtual nodes give each server many ring positions.",
                                "title": "Virtual nodes",
                                "concept_key": "virtual-nodes",
                            },
                        ],
                    )
                    rec = ext2["reconciliation"]
                    check(rec["status"] == "complete", f"reconciliation: {rec}")
                    changes = [d["change"] for d in rec["deltas"]]
                    check(changes == ["unchanged", "new"], f"changes: {changes}")
                    check(rec["concept_ids"][0] == concept_id, f"ids: {rec['concept_ids']}")
                    again = await _tool(mcp, "jolt_continue_reconciliation", source_id=source_id)
                    check(again["applied_steps"] == 2, f"continue: {again}")
                    new_concept_id = rec["concept_ids"][1]

                with rep.step("jolt_sync (done: closes the run)"):
                    final = await _tool(mcp, "jolt_sync")
                    check(final["done"] and final["run_id"] == run_id, f"final plan: {final}")

        with rep.step("GET /v1/sync/status + /v1/sync/runs"):
            status = _json(await http.get("/v1/sync/status", params={"tz": "UTC"}))
            run = status["last_run"]
            check(status["health"] == "ok", f"health: {status}")
            check(run and run["id"] == run_id and run["status"] == "finished", f"run: {run}")
            check(run["client_label"] == "smoke test", f"client_label: {run['client_label']}")
            counts = (run["sources_processed"], run["questions_written"], run["gradings_submitted"])
            check(counts == (1, 1, 1), f"run counts (sources, questions, gradings): {counts}")
            check(
                [(c["kind"], c["concept_id"]) for c in run["changes"]]
                == [
                    ("concept_added", concept_id),
                    ("question_rewritten", concept_id),
                    ("concept_added", new_concept_id),
                ],
                f"changes: {run['changes']}",
            )
            runs = _json(await http.get("/v1/sync/runs", params={"limit": 5}))
            check([r["id"] for r in runs] == [run_id], f"runs: {[r['id'] for r in runs]}")
            out["sync_run_id"] = run_id

        with rep.step("GET /v1/sync/prompt"):
            prompt = _json(await http.get("/v1/sync/prompt"))
            check("jolt_sync" in prompt["prompt"], "prompt does not mention jolt_sync")
            check(prompt["schedule_time"] and prompt["version"], f"prompt meta: {prompt}")
            out["prompt_version"] = prompt["version"]

        if not skip_dsa:
            await _dsa_steps(http, rep, out, required=require_dsa)

        with rep.step("MCP with ?token= (URL-only connector)"):
            url = f"{base_url}/mcp/sse?token={mcp_token}"
            async with sse_client(url, timeout=timeout) as streams:
                async with ClientSession(*streams) as mcp:
                    await mcp.initialize()
                    tracks = await _tool(mcp, "jolt_get_tracks")
                    check(
                        any(t["id"] == track_id for t in tracks["tracks"]),
                        f"track {track_id} not visible over ?token=",
                    )

        with rep.step("GET /v1/me (onboarding progress)"):
            onboarding = _json(await http.get("/v1/me"))["onboarding"]
            check(onboarding["has_track"] and onboarding["has_mcp_token"], f"{onboarding}")
            check(onboarding["first_sync_at"], f"first_sync_at not set: {onboarding}")
            listed = _json(await http.get("/v1/mcp-tokens"))
            check([t["id"] for t in listed] == [mcp_token_id], f"mcp tokens: {listed}")
            check(listed[0]["last_used_at"], "MCP token has no last_used_at after use")

        with rep.step("DELETE /v1/mcp-tokens/{id} (revoked at once)"):
            resp = await http.delete(f"/v1/mcp-tokens/{mcp_token_id}")
            check(resp.status_code == 204, f"revoke -> {resp.status_code}: {resp.text[:200]}")
            status = await _sse_status(base_url, mcp_token, timeout)
            check(status == 401, f"GET /mcp/sse with the revoked token -> {status}")

        with rep.step("POST /v1/auth/sign-out"):
            resp = await http.post("/v1/auth/sign-out")
            check(resp.status_code == 204, f"sign-out -> {resp.status_code}: {resp.text[:200]}")
            after = await http.get("/v1/me")
            check(after.status_code == 401, f"GET /v1/me after sign-out -> {after.status_code}")

    out["steps_passed"] = rep.passed
    return out


async def _dsa_steps(
    http: httpx.AsyncClient, rep: Reporter, out: dict[str, Any], *, required: bool
) -> None:
    """The curated DSA track (BE-09): enrol, read, and answer one pattern_match question."""
    catalog = _json(await http.get("/v1/tracks/catalog"))
    dsa = next((t for t in catalog if t["kind"] == "dsa_patterns"), None)
    if dsa is None and not required:
        print("[skip] curated DSA steps: no dsa_patterns track in the catalog", flush=True)
        out["dsa_track_id"] = None
        return
    with rep.step("GET /v1/tracks/catalog + POST /v1/tracks/{id}/enrol"):
        check(dsa is not None, f"no dsa_patterns track in the catalog (import it): {catalog}")
        check(dsa["origin"] == "jolt" and not dsa["enrolled"], f"catalog entry: {dsa}")
        enrolled = _json(await http.post(f"/v1/tracks/{dsa['id']}/enrol"))
        check(enrolled["enrolled"] and enrolled["track_id"] == dsa["id"], f"enrol: {enrolled}")
        out["dsa_track_id"] = dsa["id"]

    with rep.step("GET /v1/tracks/{id}/library + /decision-tree"):
        lib = _json(await http.get(f"/v1/tracks/{dsa['id']}/library"))
        check(len(lib["patterns"]) >= 2, f"library: {len(lib['patterns'])} patterns")
        check(all(p["library"]["when_to_reach"] for p in lib["patterns"]), "empty library entry")
        tree = _json(await http.get(f"/v1/tracks/{dsa['id']}/decision-tree"))
        check(tree["root"] in {n["id"] for n in tree["nodes"]}, "decision tree has no root")

    with rep.step("DSA session + one pattern_match question (title only after stage 2)"):
        session = _json(
            await http.post(
                "/v1/review-sessions",
                json={"kind": "dsa_patterns", "track_id": dsa["id"], "time_budget_minutes": 10},
            )
        )
        items = session["items"]
        check(items, f"empty DSA session: {session}")
        weight = sum(i["pattern_match"]["time_weight"] for i in items)
        check(weight <= 10, f"session weighs {weight} minutes, budget 10")
        item = items[0]
        check(item["question_type"] == "pattern_match", f"item: {item['question_type']}")
        check(item["concept_id"] == dsa["id"], "the pattern (concept) is not masked")
        s1 = _json(
            await http.post(
                f"/v1/reviews/{item['question_id']}/stage1",
                json={
                    "concept_id": item["concept_id"],
                    "free_text": "smoke",
                    "session_id": session["id"],
                },
            )
        )
        options = s1["options"]
        check(len(options) == len(lib["patterns"]), f"{len(options)} options")
        s2 = _json(
            await http.post(f"/v1/reviews/{s1['review_id']}/stage2", json={"selected_index": 0})
        )
        title = s2["question_title"]
        check(s2["match"] in ("correct", "partial", "incorrect"), f"stage 2: {s2}")
        check(title and title not in json.dumps(session), "title served before stage 2")
        check(s2["grading"] == "graded", f"pattern_match grading: {s2['grading']}")
        out["dsa_match"] = s2["match"]


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
    parser.add_argument(
        "--dev-auth-secret",
        default=os.environ.get("JOLT_DEV_AUTH_SECRET") or None,
        help="the server's DEV_AUTH_SECRET (default: $JOLT_DEV_AUTH_SECRET); "
        "not needed for a memory-mode server without one",
    )
    parser.add_argument("--skip-dsa", action="store_true", help="leave out the curated DSA steps")
    parser.add_argument(
        "--require-dsa",
        action="store_true",
        help="fail when the catalog has no DSA track (default: skip those steps)",
    )
    args = parser.parse_args(argv)

    print(f"Jolt smoke test against {args.base_url}", flush=True)
    try:
        out = asyncio.run(
            run(
                args.base_url,
                wait_seconds=args.wait,
                expect_version=args.expect_version,
                timeout=args.timeout,
                dev_auth_secret=args.dev_auth_secret,
                skip_dsa=args.skip_dsa,
                require_dsa=args.require_dsa,
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
