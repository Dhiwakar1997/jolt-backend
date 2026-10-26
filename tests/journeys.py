"""Shared helpers for the BE-07 API-level tests (not a test module).

The app side runs through the real FastAPI app over httpx; the agent side
through the real MCP tool functions, each result validated against its output
schema in `jolt/mcp/schemas.py`.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import httpx

from jolt.data.memory import InMemoryContainer
from jolt.mcp import server as mcp_server
from jolt.mcp.schemas import validate_output

TOOLS = dict(mcp_server._TOOLS)
LONG = (
    "Consistent hashing puts keys and nodes on one ring; each key belongs to the next "
    "node clockwise, so adding a node only moves the keys between it and its predecessor."
)


async def call(name: str, **kwargs) -> dict:
    result = await TOOLS[name](**kwargs)
    validate_output(name, result)
    return result


async def ok(resp: httpx.Response, status: int = 200) -> dict:
    assert resp.status_code == status, resp.text
    return resp.json()


def question(stem: str) -> dict:
    return {
        "stem": stem,
        "options": ["right", "wrong 1", "wrong 2", "wrong 3"],
        "correct_index": 0,
        "expected_answer": f"The full answer to: {stem}",
    }


async def new_track(api, name: str = "Systems", **body) -> str:
    return (await ok(await api.post("/v1/tracks", json={"name": name, **body})))["id"]


async def new_concept(
    track_id: str,
    title: str,
    *,
    questions: int = 1,
    key: str | None = None,
    source_id: str | None = None,
    page: int | None = None,
) -> tuple[str, list[str]]:
    concept = {"text": f"{title} explained.", "title": title, "track_id": track_id}
    if key:
        concept["concept_key"] = key
    if source_id:
        concept["source_id"] = source_id
    if page:
        concept["page"] = page
    logged = await call(
        "jolt_log_session", source_ids=[source_id] if source_id else [], concepts=[concept]
    )
    (concept_id,) = logged["concept_ids"]
    ids: list[str] = []
    if questions:
        created = await call(
            "jolt_create_questions",
            concept_id=concept_id,
            questions=[question(f"{title} Q{i + 1}?") for i in range(questions)],
        )
        ids = created["question_ids"]
    return concept_id, ids


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def upload(api, runtime, track_id: str | None, *, title="Lecture 4", data=b"\xff\xd8 p1"):
    """Request, PUT and confirm a one-page photo note. Returns the source id."""
    body = {
        "title": title,
        "pages": [
            {
                "filename": "p.jpg",
                "content_type": "image/jpeg",
                "size": len(data),
                "sha256": sha(data),
            }
        ],
    }
    if track_id:
        body["track_id"] = track_id
    ticket = await ok(await api.post("/v1/uploads/request", json=body))
    doc = next(
        d for d in runtime.cosmos.container("sources").all_items() if d["id"] == ticket["source_id"]
    )
    runtime.blob.put(doc["pages"][0]["blob_path"], data, "image/jpeg")
    checks = [{"index": 0, "sha256": sha(data), "size": len(data)}]
    await ok(await api.post(f"/v1/uploads/{ticket['source_id']}/confirm", json={"pages": checks}))
    return ticket["source_id"]


async def read_source(source_id: str, markdown: str = "# Notes", **kwargs) -> dict:
    """Claim (if offered) and store a read of the source."""
    await call("jolt_get_unprocessed_sources", limit=50)
    return await call("jolt_store_extraction", source_id=source_id, markdown=markdown, **kwargs)


async def answer(api, item: dict, *, correct=True, text="short", latency=10_000):
    s1 = await ok(
        await api.post(
            f"/v1/reviews/{item['question_id']}/stage1",
            json={"concept_id": item["concept_id"], "free_text": text},
        )
    )
    s2 = await ok(
        await api.post(
            f"/v1/reviews/{s1['review_id']}/stage2",
            json={"selected_index": 0 if correct else 1, "latency_ms": latency},
        )
    )
    return s1, s2


async def set_card(runtime, user_id: str, concept_id: str, **fields) -> None:
    state = await runtime.repos.concept_states.for_concept(user_id, concept_id)
    for k, v in fields.items():
        setattr(state, k, v)
    assert await runtime.repos.concept_states.replace(state, etag=state.etag) is not None


async def make_due(runtime, user_id: str, concept_id: str) -> None:
    await set_card(
        runtime, user_id, concept_id, due_at=datetime.now(timezone.utc) - timedelta(minutes=1)
    )


async def other_user(api) -> dict:
    """A second signed-in user: {headers, user_id}."""
    signed = await ok(await api.post("/v1/auth/dev", json={"email": "eve@example.com"}))
    return {
        "headers": {"Authorization": f"Bearer {signed['app_token']}"},
        "user_id": signed["user"]["id"],
    }


@contextmanager
def count_cosmos_calls(monkeypatch):
    """Count point reads and queries against the in-memory Cosmos, per container."""
    counts: Counter = Counter()
    read_item = InMemoryContainer.read_item
    query_items = InMemoryContainer.query_items

    async def counting_read(self, *args, **kwargs):
        counts[("read", self.id)] += 1
        return await read_item(self, *args, **kwargs)

    def counting_query(self, *args, **kwargs):
        counts[("query", self.id)] += 1
        return query_items(self, *args, **kwargs)

    monkeypatch.setattr(InMemoryContainer, "read_item", counting_read)
    monkeypatch.setattr(InMemoryContainer, "query_items", counting_query)
    try:
        yield counts
    finally:
        monkeypatch.setattr(InMemoryContainer, "read_item", read_item)
        monkeypatch.setattr(InMemoryContainer, "query_items", query_items)
