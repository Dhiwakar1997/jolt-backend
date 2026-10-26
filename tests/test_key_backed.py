"""BE-11: key-backed feedback and read now, against fake providers.

Every model call goes through `runtime.model_transport`, an `httpx.MockTransport`
answering like the provider (scripts/fake_provider.py's `canned()`), so nothing
here touches the network. The "free path" tests leave the transport unset (the
real network) and make the real network transport fail loudly: without a key
no request may even be attempted.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from jolt import telemetry
from jolt.credentials.providers import (
    AnthropicAdapter,
    ModelCallError,
    OpenAIAdapter,
    parse_json_answer,
)
from jolt.domain.models import ModelUsage, ProcessingStatus
from jolt.main import create_app
from jolt.services.read_now import sniff
from tests.journeys import call, new_concept, new_track, ok, other_user, sha

ROOT = Path(__file__).resolve().parent.parent
ANTHROPIC_KEY = "sk-fake-ant-api03-TESTKEY-0001"
OPENAI_KEY = "sk-fake-proj-TESTKEY-0002"

JPEG = b"\xff\xd8\xff\xe0" + b"jpeg page bytes" * 10
PNG = b"\x89PNG\r\n\x1a\n" + b"png page bytes" * 10
PDF = b"%PDF-1.7\n" + b"pdf page bytes" * 10


def _load_fake():
    spec = importlib.util.spec_from_file_location(
        "jolt_fake_provider", ROOT / "scripts" / "fake_provider.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FAKE = _load_fake()


class FakeProvider:
    """Records every request; answers with `canned()` unless `override` does."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.override = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.override is not None:
            answer = self.override(request)
            if answer is not None:
                return answer
        body = json.loads(request.content) if request.content else None
        status, payload = FAKE.canned(request.method, request.url.path, dict(request.headers), body)
        return httpx.Response(status, json=payload)

    @property
    def model_calls(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]

    def body(self, n: int = -1) -> dict:
        return json.loads(self.model_calls[n].content)


@pytest.fixture
def fake(runtime) -> FakeProvider:
    provider = FakeProvider()
    runtime.model_transport = httpx.MockTransport(provider.handler)
    return provider


@pytest.fixture
async def api(settings, runtime, mcp_user):
    app = create_app(settings=settings, runtime=runtime)
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {mcp_user.app_token}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        yield c


@pytest.fixture
def no_network(monkeypatch) -> list:
    """The real network transport records and refuses every request."""
    attempts: list = []

    async def refuse(self, request):
        attempts.append(request)
        raise AssertionError(f"outbound HTTP attempted: {request.url}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)
    return attempts


async def add_key(api, provider: str, key: str) -> None:
    await ok(await api.post("/v1/settings/api-key", json={"provider": provider, "key": key}))


async def answered_concept(api, *, words: str = "it spreads load across servers") -> dict:
    """A concept with one question, answered once in the learner's words."""
    track_id = await new_track(api)
    concept_id, _ = await new_concept(track_id, "Consistent hashing")
    review = await answer_once(api, concept_id, words)
    return {"track_id": track_id, "concept_id": concept_id, "review_id": review}


async def answer_once(api, concept_id: str, words: str, *, correct: bool = True) -> str:
    session = await ok(await api.post("/v1/review-sessions", json={"concept_id": concept_id}))
    item = session["items"][0]
    s1 = await ok(
        await api.post(
            f"/v1/reviews/{item['question_id']}/stage1",
            json={"concept_id": item["concept_id"], "free_text": words},
        )
    )
    await ok(
        await api.post(
            f"/v1/reviews/{s1['review_id']}/stage2",
            json={"selected_index": 0 if correct else 1},
        )
    )
    return s1["review_id"]


async def upload_pages(api, runtime, track_id, pages: list[tuple[str, bytes]]) -> str:
    """A confirmed multi-page note: [(content_type, bytes)] in page order."""
    ext = {"image/jpeg": "jpg", "image/png": "png", "application/pdf": "pdf", "image/heic": "heic"}
    body: dict = {
        "title": "Lecture 7",
        "pages": [
            {
                "filename": f"p{i + 1}.{ext[ct]}",
                "content_type": ct,
                "size": len(data),
                "sha256": sha(data),
            }
            for i, (ct, data) in enumerate(pages)
        ],
    }
    if track_id:
        body["track_id"] = track_id
    ticket = await ok(await api.post("/v1/uploads/request", json=body))
    doc = next(
        d for d in runtime.cosmos.container("sources").all_items() if d["id"] == ticket["source_id"]
    )
    for page, (ct, data) in zip(doc["pages"], pages):
        runtime.blob.put(page["blob_path"], data, ct)
    checks = [{"index": i, "sha256": sha(d), "size": len(d)} for i, (_, d) in enumerate(pages)]
    await ok(await api.post(f"/v1/uploads/{ticket['source_id']}/confirm", json={"pages": checks}))
    return ticket["source_id"]


def error(resp: httpx.Response, status: int, code: str) -> dict:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body["code"] == code, body
    assert isinstance(body["detail"], str) and body["detail"]
    return body


# --------------------------------------------------------------------------- #
# Feedback
# --------------------------------------------------------------------------- #
async def test_feedback_with_an_anthropic_key(api, runtime, fake):
    await add_key(api, "anthropic", ANTHROPIC_KEY)
    c = await answered_concept(api)

    resp = await ok(await api.post(f"/v1/concepts/{c['concept_id']}/feedback"))
    assert resp["feedback"] == FAKE.FEEDBACK["feedback"]
    assert resp["next_step"] == FAKE.FEEDBACK["next_step"]
    assert resp["provider"] == "anthropic" and resp["cached"] is False
    assert resp["model_id"] == f"{runtime.settings.anthropic_model}-fake"
    assert resp["answers"] == 1 and resp["review_id"] is None
    assert resp["usage"] == {
        "input_tokens": 1200,
        "output_tokens": 300,
        "requests_today": 1,
        "daily_request_cap": runtime.settings.model_daily_request_cap,
        "tokens_today": 1500,
        "daily_token_cap": runtime.settings.model_daily_token_cap,
    }

    (req,) = fake.model_calls
    assert str(req.url) == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == ANTHROPIC_KEY
    assert req.headers["anthropic-version"] == "2026-10-21"
    assert req.headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    body = fake.body()
    assert body["model"] == runtime.settings.anthropic_model
    assert body["output_config"] == {"effort": "low"} and body["fallbacks"] == "default"
    assert "feedback coach" in body["system"] and "Add no facts" in body["system"]
    assert "temperature" not in body
    message = body["messages"][0]["content"][0]["text"]
    # Grounded in the timeline: the concept text, the question, the learner's words.
    assert "Consistent hashing explained." in message
    assert "it spreads load across servers" in message
    assert "Consistent hashing Q1?" in message and "Expected answer" in message


async def test_feedback_with_an_openai_key(api, runtime, fake):
    await add_key(api, "openai", OPENAI_KEY)
    c = await answered_concept(api)

    resp = await ok(await api.post(f"/v1/concepts/{c['concept_id']}/feedback"))
    assert resp["provider"] == "openai" and resp["feedback"] == FAKE.FEEDBACK["feedback"]
    assert resp["model_id"] == f"{runtime.settings.openai_model}-fake"
    assert resp["usage"]["input_tokens"] == 1100 and resp["usage"]["output_tokens"] == 250

    (req,) = fake.model_calls
    assert str(req.url) == "https://api.openai.com/v1/responses"
    assert req.headers["authorization"] == f"Bearer {OPENAI_KEY}"
    body = fake.body()
    assert body["model"] == runtime.settings.openai_model
    assert body["store"] is False and body["reasoning"] == {"effort": "low"}
    assert body["text"] == {"format": {"type": "json_object"}}
    assert "feedback coach" in body["instructions"]
    assert body["input"][0]["content"][0]["type"] == "input_text"


async def test_feedback_is_cached_until_the_next_answer(api, runtime, fake):
    await add_key(api, "anthropic", ANTHROPIC_KEY)
    c = await answered_concept(api)
    url = f"/v1/concepts/{c['concept_id']}/feedback"

    first = await ok(await api.post(url))
    again = await ok(await api.post(url))
    assert again["cached"] is True and len(fake.model_calls) == 1
    assert again["feedback"] == first["feedback"]
    assert again["generated_at"] == first["generated_at"]
    assert again["usage"]["input_tokens"] == 0 and again["usage"]["requests_today"] == 1

    # A new answer is a new timeline: the next request asks the model again.
    await answer_once(api, c["concept_id"], "only the neighbouring keys move")
    fresh = await ok(await api.post(url))
    assert fresh["cached"] is False and fresh["answers"] == 2
    assert len(fake.model_calls) == 2
    assert "only the neighbouring keys move" in fake.body()["messages"][0]["content"][0]["text"]

    # A grade landing from the nightly sync changes the timeline too.
    pending = await call("jolt_get_pending_gradings", limit=10)
    await call(
        "jolt_submit_gradings",
        gradings=[
            {"review_id": g["review_id"], "suggested_fsrs_grade": 3, "rationale": "Good."}
            for g in pending["gradings"]
        ],
    )
    regraded = await ok(await api.post(url))
    assert regraded["cached"] is False and len(fake.model_calls) == 3


async def test_explain_one_answer_has_its_own_cache(api, runtime, fake):
    await add_key(api, "anthropic", ANTHROPIC_KEY)
    c = await answered_concept(api)
    url = f"/v1/concepts/{c['concept_id']}/feedback"

    explained = await ok(await api.post(url, json={"review_id": c["review_id"]}))
    assert explained["review_id"] == c["review_id"]
    body = fake.body()
    assert "FOCUS" in body["messages"][0]["content"][0]["text"]
    assert "marked FOCUS" in body["system"]

    await ok(await api.post(url))  # the whole-timeline feedback: another slot
    assert len(fake.model_calls) == 2
    assert (await ok(await api.post(url, json={"review_id": c["review_id"]})))["cached"] is True
    assert (await ok(await api.post(url)))["cached"] is True
    assert len(fake.model_calls) == 2

    missing = await api.post(url, json={"review_id": "0" * 32})
    assert missing.status_code == 404


async def test_feedback_uses_the_newest_key_unless_a_provider_is_named(api, runtime, fake):
    await add_key(api, "anthropic", ANTHROPIC_KEY)
    await add_key(api, "openai", OPENAI_KEY)
    c = await answered_concept(api)
    url = f"/v1/concepts/{c['concept_id']}/feedback"

    newest = await ok(await api.post(url))
    assert newest["provider"] == "openai"
    await answer_once(api, c["concept_id"], "a second try")
    named = await ok(await api.post(url, json={"provider": "anthropic"}))
    assert named["provider"] == "anthropic"
    assert fake.model_calls[-1].url.path == "/v1/messages"


async def test_feedback_needs_an_answer(api, runtime, fake):
    await add_key(api, "anthropic", ANTHROPIC_KEY)
    track_id = await new_track(api)
    concept_id, _ = await new_concept(track_id, "Unanswered")
    error(await api.post(f"/v1/concepts/{concept_id}/feedback"), 409, "no_answers")
    assert fake.model_calls == []


# --------------------------------------------------------------------------- #
# Read now
# --------------------------------------------------------------------------- #
async def test_read_now_with_an_anthropic_key_sends_every_page(api, runtime, fake):
    await add_key(api, "anthropic", ANTHROPIC_KEY)
    track_id = await new_track(api, outline="Consistent hashing")
    source_id = await upload_pages(
        api, runtime, track_id, [("image/jpeg", JPEG), ("image/png", PNG), ("application/pdf", PDF)]
    )

    resp = await ok(await api.post(f"/v1/sources/{source_id}/read-now"))
    assert resp["provider"] == "anthropic" and resp["reconciliation"] == "complete"
    assert resp["model_id"] == f"{runtime.settings.anthropic_model}-fake"
    assert resp["confidence"] == 0.9 and resp["low_confidence"] is False
    assert resp["questions_written"] == 1 and resp["removed_concept_ids"] == []
    (concept,) = resp["concepts"]
    assert concept["change"] == "new" and concept["questions_written"] == 1

    body = fake.body()
    content = body["messages"][0]["content"]
    assert [b["type"] for b in content] == ["image", "image", "document", "text"]
    assert content[0]["source"] == {
        "type": "base64",
        "media_type": "image/jpeg",
        "data": base64.b64encode(JPEG).decode(),
    }
    assert content[1]["source"]["media_type"] == "image/png"
    assert content[2]["source"]["media_type"] == "application/pdf"
    assert base64.b64decode(content[2]["source"]["data"]) == PDF
    assert body["output_config"] == {"effort": "medium"}
    assert "Transcribe faithfully" in body["system"] and "exactly 4" in body["system"]
    assert "consistent-hashing" in content[3]["text"]  # the agenda keys to reuse

    # Stored through the sync's path: one active read, the model's id, the label.
    detail = await ok(await api.get(f"/v1/sources/{source_id}"))
    assert detail["processing_status"] == "processed"
    read = detail["active_read"]
    assert read["via"] == "read_now" and read["client_label"] == "Read now"
    assert read["model_id"] == resp["model_id"] and read["markdown"].startswith("# Consistent")
    assert read["concepts_found"] == 1 and read["questions_written"] == 1
    extractions = await ok(await api.get(f"/v1/sources/{source_id}/extractions"))
    assert [e["via"] for e in extractions] == ["read_now"]

    # The concept is reviewable right away, filed under the agenda key.
    track = await ok(await api.get(f"/v1/tracks/{track_id}"))
    row = next(i for i in track["items"] if i["concept_id"] == concept["concept_id"])
    assert row["question_count"] == 1 and row["concept_key"] == "consistent-hashing"
    session = await ok(await api.post("/v1/review-sessions", json={}))
    assert [i["concept_id"] for i in session["items"]] == [concept["concept_id"]]


async def test_read_now_with_an_openai_key(api, runtime, fake):
    await add_key(api, "openai", OPENAI_KEY)
    track_id = await new_track(api)
    source_id = await upload_pages(
        api, runtime, track_id, [("image/jpeg", JPEG), ("application/pdf", PDF)]
    )

    resp = await ok(await api.post(f"/v1/sources/{source_id}/read-now"))
    assert resp["provider"] == "openai" and len(resp["concepts"]) == 1
    body = fake.body()
    content = body["input"][0]["content"]
    assert [c["type"] for c in content] == ["input_image", "input_file", "input_text"]
    assert content[0]["image_url"] == "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()
    assert content[1]["filename"] == "p2.pdf"
    assert content[1]["file_data"].startswith("data:application/pdf;base64,")
    assert body["reasoning"] == {"effort": "medium"} and body["store"] is False
    detail = await ok(await api.get(f"/v1/sources/{source_id}"))
    assert detail["active_read"]["model_id"] == f"{runtime.settings.openai_model}-fake"


async def test_read_now_again_reconciles_and_keeps_one_active_read(api, runtime, fake):
    await add_key(api, "anthropic", ANTHROPIC_KEY)
    track_id = await new_track(api)
    source_id = await upload_pages(api, runtime, track_id, [("image/jpeg", JPEG)])
    first = await ok(await api.post(f"/v1/sources/{source_id}/read-now"))
    concept_id = first["concepts"][0]["concept_id"]

    again = await ok(await api.post(f"/v1/sources/{source_id}/read-now"))
    assert [c["change"] for c in again["concepts"]] == ["unchanged"]
    assert again["concepts"][0]["concept_id"] == concept_id
    assert again["questions_written"] == 0  # the kept concept keeps its questions
    assert "read before" in fake.body()["messages"][0]["content"][-1]["text"]

    detail = await ok(await api.get(f"/v1/sources/{source_id}"))
    assert [r["status"] for r in detail["reads"]] == ["active", "superseded"]
    assert detail["active_extraction_id"] == again["extraction_id"]
    track = await ok(await api.get(f"/v1/tracks/{track_id}"))
    assert track["concept_count"] == 1


async def test_read_now_answers_a_pending_read_again_request(api, runtime, fake):
    await add_key(api, "anthropic", ANTHROPIC_KEY)
    track_id = await new_track(api)
    source_id = await upload_pages(api, runtime, track_id, [("image/jpeg", JPEG)])
    await ok(await api.post(f"/v1/sources/{source_id}/read-now"))
    await ok(await api.post(f"/v1/sources/{source_id}/reread", json={"preferred_model": "claude"}))

    await ok(await api.post(f"/v1/sources/{source_id}/read-now"))
    detail = await ok(await api.get(f"/v1/sources/{source_id}"))
    assert detail["reread_requested"] is None and detail["processing_status"] == "processed"
    plan = await call("jolt_sync")
    assert plan["unprocessed_sources"] == 0 and plan["rereads"] == 0


async def test_read_now_refuses_a_source_a_sync_holds(api, runtime, fake, mcp_user):
    await add_key(api, "anthropic", ANTHROPIC_KEY)
    track_id = await new_track(api)
    source_id = await upload_pages(api, runtime, track_id, [("image/jpeg", JPEG)])
    claimed = await call("jolt_get_unprocessed_sources", limit=5)
    assert [s["source_id"] for s in claimed["sources"]] == [source_id]

    error(await api.post(f"/v1/sources/{source_id}/read-now"), 409, "source_busy")
    assert fake.model_calls == []


async def test_a_failed_read_now_gives_the_source_back_to_the_sync(api, runtime, fake):
    await add_key(api, "anthropic", ANTHROPIC_KEY)
    track_id = await new_track(api)
    source_id = await upload_pages(api, runtime, track_id, [("image/jpeg", JPEG)])
    fake.override = lambda r: httpx.Response(500, json={"error": {"type": "api_error"}})

    error(await api.post(f"/v1/sources/{source_id}/read-now"), 502, "provider_error")
    source = await runtime.repos.sources.get_source(source_id, await _owner(runtime))
    assert source.processing_status == ProcessingStatus.UNPROCESSED
    assert source.lease_expires_at is None and source.active_extraction_id is None
    claimed = await call("jolt_get_unprocessed_sources", limit=5)
    assert [s["source_id"] for s in claimed["sources"]] == [source_id]


async def _owner(runtime) -> str:
    (doc,) = runtime.cosmos.container("sources").all_items()
    return doc["user_id"]


async def test_read_now_files_concepts_on_the_only_track_when_the_note_has_none(api, runtime, fake):
    await add_key(api, "anthropic", ANTHROPIC_KEY)
    source_id = await upload_pages(api, runtime, None, [("image/jpeg", JPEG)])
    error(await api.post(f"/v1/sources/{source_id}/read-now"), 409, "track_required")
    assert fake.model_calls == []

    track_id = await new_track(api, "Only track")
    resp = await ok(await api.post(f"/v1/sources/{source_id}/read-now"))
    context = fake.body()["messages"][0]["content"][-1]["text"]
    assert "no track yet" in context and track_id in context
    concept = await ok(await api.get(f"/v1/concepts/{resp['concepts'][0]['concept_id']}"))
    assert concept["track_id"] == track_id


async def test_read_now_size_and_type_limits(api, runtime, fake):
    await add_key(api, "anthropic", ANTHROPIC_KEY)
    track_id = await new_track(api)
    runtime.settings.read_now_max_image_bytes = 100
    big = await upload_pages(api, runtime, track_id, [("image/jpeg", JPEG + b"x" * 200)])
    error(await api.post(f"/v1/sources/{big}/read-now"), 413, "source_too_large")

    runtime.settings.read_now_max_image_bytes = 10_000
    runtime.settings.read_now_max_total_bytes = 300
    many = await upload_pages(
        api, runtime, track_id, [("image/jpeg", JPEG), ("application/pdf", PDF + b"x" * 300)]
    )
    error(await api.post(f"/v1/sources/{many}/read-now"), 413, "source_too_large")

    runtime.settings.read_now_max_total_bytes = 10_000
    heic = await upload_pages(api, runtime, track_id, [("image/heic", b"\x00\x00\x00 ftypheic")])
    error(await api.post(f"/v1/sources/{heic}/read-now"), 415, "unsupported_page_type")

    # Declared a JPEG, but the bytes are not one: refused after the claim, and
    # the source goes back to the queue.
    fake_jpeg = await upload_pages(api, runtime, track_id, [("image/jpeg", b"<html>hi</html>")])
    error(await api.post(f"/v1/sources/{fake_jpeg}/read-now"), 415, "unsupported_page_type")
    detail = await ok(await api.get(f"/v1/sources/{fake_jpeg}"))
    assert detail["processing_status"] == "unprocessed"
    assert fake.model_calls == []


# --------------------------------------------------------------------------- #
# The free path: no key, no outbound call
# --------------------------------------------------------------------------- #
async def test_without_a_key_no_route_attempts_an_outbound_call(api, runtime, no_network):
    assert runtime.model_transport is None  # the real network transport
    c = await answered_concept(api)
    track_id = await new_track(api, "Second")
    source_id = await upload_pages(api, runtime, track_id, [("image/jpeg", JPEG)])

    for url, payload in [
        (f"/v1/concepts/{c['concept_id']}/feedback", None),
        (f"/v1/concepts/{c['concept_id']}/feedback", {"review_id": c["review_id"]}),
        (f"/v1/concepts/{c['concept_id']}/feedback", {"provider": "openai"}),
        (f"/v1/sources/{source_id}/read-now", None),
        (f"/v1/sources/{source_id}/read-now", {"provider": "anthropic"}),
    ]:
        error(await api.post(url, json=payload), 403, "key_required")
    assert no_network == []
    detail = await ok(await api.get(f"/v1/sources/{source_id}"))
    assert detail["processing_status"] == "unprocessed"  # not claimed either


async def test_a_key_for_the_other_provider_is_not_used(api, runtime, fake):
    await add_key(api, "openai", OPENAI_KEY)
    c = await answered_concept(api)
    before = len(fake.requests)
    resp = await api.post(
        f"/v1/concepts/{c['concept_id']}/feedback", json={"provider": "anthropic"}
    )
    error(resp, 403, "key_required")
    assert len(fake.requests) == before


async def test_key_backed_routes_check_ownership_first(api, runtime, fake):
    await add_key(api, "anthropic", ANTHROPIC_KEY)
    c = await answered_concept(api)
    track_id = await new_track(api, "Second")
    source_id = await upload_pages(api, runtime, track_id, [("image/jpeg", JPEG)])
    eve = await other_user(api)
    await ok(
        await api.post(
            "/v1/settings/api-key",
            json={"provider": "anthropic", "key": "sk-fake-eve-0003"},
            headers=eve["headers"],
        )
    )
    calls = len(fake.model_calls)
    for url in (
        f"/v1/concepts/{c['concept_id']}/feedback",
        f"/v1/sources/{source_id}/read-now",
        "/v1/concepts/unknown/feedback",
        "/v1/sources/unknown/read-now",
    ):
        resp = await api.post(url, headers=eve["headers"])
        assert resp.status_code == 404, (url, resp.text)
    assert len(fake.model_calls) == calls
    unauth = await api.post(f"/v1/sources/{source_id}/read-now", headers={"Authorization": ""})
    assert unauth.status_code == 401


async def test_request_validation_of_the_key_backed_bodies(api, runtime, fake):
    await add_key(api, "anthropic", ANTHROPIC_KEY)
    c = await answered_concept(api)
    url = f"/v1/concepts/{c['concept_id']}/feedback"
    assert (await api.post(url, json={"provider": "gemini"})).status_code == 422
    assert (await api.post(url, json={"review_id": "x" * 65})).status_code == 422
    assert fake.model_calls == []


# --------------------------------------------------------------------------- #
# Provider errors
# --------------------------------------------------------------------------- #
async def test_an_invalid_key_is_key_invalid_and_never_echoed(api, runtime, fake):
    await add_key(api, "openai", OPENAI_KEY)
    c = await answered_concept(api)
    track_id = await new_track(api, "Second")
    source_id = await upload_pages(api, runtime, track_id, [("image/jpeg", JPEG)])
    # The real OpenAI 401 repeats part of the key in its message.
    fake.override = lambda r: httpx.Response(
        401, json={"error": {"message": f"Incorrect API key provided: {OPENAI_KEY}"}}
    )

    for url in (f"/v1/concepts/{c['concept_id']}/feedback", f"/v1/sources/{source_id}/read-now"):
        resp = await api.post(url)
        body = error(resp, 422, "key_invalid")
        assert OPENAI_KEY not in resp.text and OPENAI_KEY[:12] not in body["detail"]
    detail = await ok(await api.get(f"/v1/sources/{source_id}"))
    assert detail["processing_status"] == "unprocessed"


@pytest.mark.parametrize(
    ("provider", "key", "response", "status", "code"),
    [
        (
            "anthropic",
            ANTHROPIC_KEY,
            httpx.Response(400, json={"error": {"message": "Your credit balance is too low"}}),
            402,
            "out_of_credit",
        ),
        ("anthropic", ANTHROPIC_KEY, httpx.Response(402, json={}), 402, "out_of_credit"),
        (
            "openai",
            OPENAI_KEY,
            httpx.Response(429, json={"error": {"code": "insufficient_quota"}}),
            402,
            "out_of_credit",
        ),
        (
            "openai",
            OPENAI_KEY,
            httpx.Response(429, headers={"retry-after": "12"}, json={"error": {}}),
            429,
            "rate_limited",
        ),
        ("anthropic", ANTHROPIC_KEY, httpx.Response(529, json={}), 502, "provider_error"),
        ("anthropic", ANTHROPIC_KEY, httpx.Response(403, json={}), 422, "key_invalid"),
        ("openai", OPENAI_KEY, httpx.Response(404, json={}), 502, "provider_error"),
    ],
)
async def test_provider_failures_are_typed(
    api, runtime, fake, provider, key, response, status, code
):
    await add_key(api, provider, key)
    c = await answered_concept(api)
    fake.override = lambda r: response if r.method == "POST" else None
    resp = await api.post(f"/v1/concepts/{c['concept_id']}/feedback")
    error(resp, status, code)
    if code == "rate_limited":
        assert resp.headers["retry-after"] == "12"
    # A failed call still counts as a request on the user's key.
    user = await runtime.repos.users.get_user(await _user_id(api))
    assert user.model_usage.requests == 1 and user.model_usage.tokens == 0


async def _user_id(api) -> str:
    return (await ok(await api.get("/v1/me")))["user"]["id"]


async def test_timeouts_refusals_and_unreadable_answers(api, runtime, fake):
    await add_key(api, "anthropic", ANTHROPIC_KEY)
    c = await answered_concept(api)
    url = f"/v1/concepts/{c['concept_id']}/feedback"

    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    fake.override = timeout
    error(await api.post(url), 504, "provider_error")

    fake.override = lambda r: httpx.Response(
        200, json={"content": [], "stop_reason": "refusal", "usage": {}}
    )
    error(await api.post(url), 502, "provider_error")

    fake.override = lambda r: httpx.Response(
        200, json={"content": [{"type": "text", "text": "Sorry, no JSON"}], "usage": {}}
    )
    error(await api.post(url), 502, "provider_error")
    # None of these was cached.
    fake.override = None
    assert (await ok(await api.post(url)))["cached"] is False


# --------------------------------------------------------------------------- #
# Spend guard
# --------------------------------------------------------------------------- #
async def test_spend_guard_caps_requests_per_day(api, runtime, fake):
    runtime.settings.model_daily_request_cap = 2
    await add_key(api, "anthropic", ANTHROPIC_KEY)
    c = await answered_concept(api)
    url = f"/v1/concepts/{c['concept_id']}/feedback"
    track_id = await new_track(api, "Second")
    source_id = await upload_pages(api, runtime, track_id, [("image/jpeg", JPEG)])

    await ok(await api.post(url))
    await answer_once(api, c["concept_id"], "again")
    await ok(await api.post(url))
    assert (await ok(await api.post(url)))["cached"] is True  # the cache is free

    await answer_once(api, c["concept_id"], "and again")
    resp = await api.post(url)
    error(resp, 429, "spend_limit")
    assert 0 < int(resp.headers["retry-after"]) <= 86400
    error(await api.post(f"/v1/sources/{source_id}/read-now"), 429, "spend_limit")
    assert len(fake.model_calls) == 2
    detail = await ok(await api.get(f"/v1/sources/{source_id}"))
    assert detail["processing_status"] == "unprocessed"

    # A new UTC day starts from zero.
    user = await runtime.repos.users.get_user(await _user_id(api))
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    user.model_usage = ModelUsage(day=yesterday, requests=2, input_tokens=10)
    await runtime.repos.users.replace(user, etag=user.etag)
    resp = await ok(await api.post(url))
    assert resp["usage"]["requests_today"] == 1


async def test_spend_guard_caps_tokens_and_zero_turns_it_off(api, runtime, fake):
    runtime.settings.model_daily_token_cap = 1000
    await add_key(api, "anthropic", ANTHROPIC_KEY)
    c = await answered_concept(api)
    url = f"/v1/concepts/{c['concept_id']}/feedback"
    await ok(await api.post(url))  # 1500 tokens: over the cap afterwards
    await answer_once(api, c["concept_id"], "again")
    error(await api.post(url), 429, "spend_limit")

    runtime.settings.model_daily_token_cap = 2_000_000
    runtime.settings.model_daily_request_cap = 0
    body = error(await api.post(url), 429, "spend_limit")
    assert "turned off" in body["detail"]
    assert len(fake.model_calls) == 1


# --------------------------------------------------------------------------- #
# Custody: the key never leaves Key Vault except in the provider header
# --------------------------------------------------------------------------- #
async def test_the_key_never_appears_in_logs_errors_telemetry_or_documents(
    api, runtime, fake, caplog
):
    reader = InMemoryMetricReader()
    telemetry.bind_meter(MeterProvider(metric_readers=[reader]).get_meter("jolt"))
    caplog.set_level(logging.DEBUG)
    try:
        await add_key(api, "anthropic", ANTHROPIC_KEY)
        await add_key(api, "openai", OPENAI_KEY)
        c = await answered_concept(api)
        track_id = await new_track(api, "Second")
        source_id = await upload_pages(api, runtime, track_id, [("image/jpeg", JPEG)])
        bodies = []
        for provider in ("anthropic", "openai"):
            payload = {"provider": provider}
            bodies.append((await api.post(f"/v1/sources/{source_id}/read-now", json=payload)).text)
            await answer_once(api, c["concept_id"], f"try with {provider}")
            bodies.append(
                (await api.post(f"/v1/concepts/{c['concept_id']}/feedback", json=payload)).text
            )
        fake.override = lambda r: httpx.Response(
            401, json={"error": {"message": f"bad key {ANTHROPIC_KEY} {OPENAI_KEY}"}}
        )
        await answer_once(api, c["concept_id"], "one more")
        bodies.append((await api.post(f"/v1/concepts/{c['concept_id']}/feedback")).text)

        def timeout(request):
            raise httpx.ConnectError(f"cannot connect {request.headers}", request=request)

        fake.override = timeout
        bodies.append((await api.post(f"/v1/sources/{source_id}/read-now")).text)
        bodies.append((await api.get("/v1/settings/api-key")).text)
        bodies.append((await api.get(f"/v1/sources/{source_id}")).text)
        bodies.append((await api.get(f"/v1/concepts/{c['concept_id']}")).text)
        export = await ok(await api.post("/v1/export"))
        assert export["bytes"] > 0

        secrets = (ANTHROPIC_KEY, OPENAI_KEY, "TESTKEY")
        logged = caplog.text + "".join(repr(r.__dict__) for r in caplog.records)
        stored = json.dumps(
            [
                runtime.cosmos.container(name).all_items()
                for name in runtime.cosmos._containers  # every Cosmos document
            ],
            default=str,
        )
        blobs = b"".join(runtime.blob.get(p).data for p in runtime.blob.paths())
        data = reader.get_metrics_data()
        attributes = [
            dict(point.attributes)
            for rm in data.resource_metrics
            for sm in rm.scope_metrics
            for metric in sm.metrics
            for point in metric.data.data_points
        ]
        assert any(a.get("kind") == "read_now" for a in attributes)
        for secret in secrets:
            assert secret not in logged
            assert all(secret not in body for body in bodies)
            assert secret not in stored
            assert secret.encode() not in blobs
            assert secret not in repr(attributes)
        # Only Key Vault holds the plaintext.
        assert sorted(runtime.vault._secrets.values()) == sorted([ANTHROPIC_KEY, OPENAI_KEY])
    finally:
        telemetry.reset_meter()


async def test_model_call_metrics(api, runtime, fake):
    reader = InMemoryMetricReader()
    telemetry.bind_meter(MeterProvider(metric_readers=[reader]).get_meter("jolt"))
    try:
        await add_key(api, "anthropic", ANTHROPIC_KEY)
        c = await answered_concept(api)
        await ok(await api.post(f"/v1/concepts/{c['concept_id']}/feedback"))
        points = {}
        for rm in reader.get_metrics_data().resource_metrics:
            for sm in rm.scope_metrics:
                for metric in sm.metrics:
                    if metric.name.startswith("jolt.model."):
                        points[metric.name] = [
                            (dict(p.attributes), p.value) for p in metric.data.data_points
                        ]
        assert points["jolt.model.calls"] == [
            ({"provider": "anthropic", "kind": "feedback", "outcome": "ok"}, 1)
        ]
        tokens = {a["direction"]: v for a, v in points["jolt.model.tokens"]}
        assert tokens == {"input": 1200, "output": 300}
    finally:
        telemetry.reset_meter()


# --------------------------------------------------------------------------- #
# Units
# --------------------------------------------------------------------------- #
def test_parse_json_answer_tolerates_fences_and_prose():
    assert parse_json_answer('{"a": 1}') == {"a": 1}
    assert parse_json_answer('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_answer('Here you go: {"a": {"b": 2}} done') == {"a": {"b": 2}}
    assert parse_json_answer("no json") is None
    assert parse_json_answer("[1, 2]") is None


def test_sniff_reads_the_real_type():
    assert (
        sniff(JPEG) == "image/jpeg"
        and sniff(PNG) == "image/png"
        and sniff(PDF) == ("application/pdf")
    )
    assert sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff(b"\x00\x00\x00\x18ftypheic") is None and sniff(b"<html>") is None


def test_adapters_map_statuses_without_repeating_the_body():
    key = "sk-secret-value-9999"
    for adapter in (AnthropicAdapter(), OpenAIAdapter()):
        resp = httpx.Response(401, json={"error": {"message": f"bad {key}"}})
        err = adapter._error_for(resp)
        assert isinstance(err, ModelCallError) and err.code == "key_invalid"
        assert key not in err.detail
    assert AnthropicAdapter()._error_for(httpx.Response(500)).code == "provider_error"
    assert (
        AnthropicAdapter()._error_for(httpx.Response(429, headers={"retry-after": "x"})).retry_after
        == 30
    )
