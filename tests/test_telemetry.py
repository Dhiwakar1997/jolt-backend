"""Observability (BE-10, jolt/telemetry.py): off by default, custom metrics,
Cosmos RU per route and per tool (0 in memory mode)."""

from __future__ import annotations

import hashlib
import sys
import types
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from jolt import telemetry
from jolt.config import Settings
from jolt.domain.models import ProcessingStatus
from jolt.main import create_app
from jolt.mcp import server as mcp_server
from jolt.runtime import Runtime
from jolt.services.sync import SyncService


@pytest.fixture
def reader():
    reader = InMemoryMetricReader()
    telemetry.bind_meter(MeterProvider(metric_readers=[reader]).get_meter("jolt"))
    yield reader
    telemetry.reset_meter()


def points(reader: InMemoryMetricReader, name: str) -> list:
    data = reader.get_metrics_data()
    out = []
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name == name:
                    out += list(metric.data.data_points)
    return out


def attrs(point) -> dict:
    return dict(point.attributes)


# --------------------------------------------------------------------------- #
# Off by default
# --------------------------------------------------------------------------- #
def test_telemetry_is_off_without_a_connection_string(monkeypatch):
    # Make the distro unimportable: nothing may try to load it.
    monkeypatch.setitem(sys.modules, "azure.monitor.opentelemetry", None)
    settings = Settings(_env_file=None, storage_mode="memory")
    assert settings.applicationinsights_connection_string == ""
    assert telemetry.configure_telemetry(settings) is False
    app = create_app(settings=settings, runtime=Runtime.in_memory(settings))
    assert app is not None
    assert telemetry.is_enabled() is False
    telemetry.flush()  # a no-op when off


def test_connection_string_turns_the_distro_on_once(monkeypatch):
    calls = []
    fake = types.ModuleType("azure.monitor.opentelemetry")
    fake.configure_azure_monitor = lambda **kwargs: calls.append(kwargs)
    monkeypatch.setitem(sys.modules, "azure.monitor.opentelemetry", fake)
    monkeypatch.setattr(telemetry, "_configured", False)
    settings = Settings(
        _env_file=None,
        storage_mode="memory",
        applicationinsights_connection_string="InstrumentationKey=00000000-0000-0000-0000-000000000000",
        jolt_env="prod",
        app_version="abc123",
    )
    try:
        assert telemetry.configure_telemetry(settings) is True
        assert telemetry.configure_telemetry(settings) is True
        assert len(calls) == 1
        kwargs = calls[0]
        assert kwargs["connection_string"].startswith("InstrumentationKey=")
        assert kwargs["logger_name"] == "jolt"  # never the access log
        resource = dict(kwargs["resource"].attributes)
        assert resource["service.name"] == "jolt-api"
        assert resource["deployment.environment"] == "prod"
        assert resource["service.version"] == "abc123"
    finally:
        monkeypatch.setattr(telemetry, "_configured", False)


# --------------------------------------------------------------------------- #
# Cosmos RU
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, charge):
        self.http_response = types.SimpleNamespace(headers={"x-ms-request-charge": charge})


def test_cosmos_hook_adds_the_request_charge_to_the_current_meter():
    telemetry.cosmos_response_hook(_FakeResponse("9.5"))  # outside a meter: ignored
    with telemetry.metering() as outer:
        telemetry.cosmos_response_hook(_FakeResponse("2.5"))
        with telemetry.metering() as inner:  # a tool call inside a stream
            telemetry.cosmos_response_hook(_FakeResponse("1"))
        telemetry.cosmos_response_hook(_FakeResponse("3.25"))
    assert outer.total == pytest.approx(5.75) and outer.requests == 2
    assert inner.total == 1.0 and inner.requests == 1


# --------------------------------------------------------------------------- #
# Request and tool metrics
# --------------------------------------------------------------------------- #
async def test_rest_requests_record_route_status_and_zero_ru_in_memory(reader, client, auth):
    assert client.get("/v1/me", headers=auth).status_code == 200
    assert client.get("/v1/sources/nope", headers=auth).status_code == 404
    client.get("/health")

    requests = {
        (a["route"], a["status_code"]) for a in map(attrs, points(reader, "jolt.http.requests"))
    }
    assert ("/v1/me", 200) in requests
    assert ("/v1/sources/{source_id}", 404) in requests  # the template, not the id
    assert not any(route == "/health" for route, _ in requests)

    charges = {attrs(p)["operation"]: p for p in points(reader, "jolt.cosmos.request_charge")}
    me = charges["GET /v1/me"]
    assert attrs(me)["surface"] == "rest"
    assert me.count >= 1 and me.sum == 0  # memory mode: no Cosmos, 0 RU


async def test_rate_limited_requests_are_counted(reader):
    settings = Settings(_env_file=None, storage_mode="memory", rate_limit_rest_per_minute=1)
    app = create_app(settings=settings, runtime=Runtime.in_memory(settings))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        await c.get("/v1/me")
        assert (await c.get("/v1/me")).status_code == 429
    limited = [attrs(p) for p in points(reader, "jolt.rate_limited")]
    assert {"surface": "rest", "scope": "ip"} in limited
    statuses = [attrs(p)["status_code"] for p in points(reader, "jolt.http.requests")]
    assert 429 in statuses


async def test_tool_calls_record_outcome_and_ru(reader, runtime, mcp_user):
    mcp = mcp_server.build_mcp()
    await mcp.call_tool("jolt_get_tracks", {})
    with pytest.raises(Exception):
        await mcp.call_tool("jolt_get_source_content", {"source_id": "nope"})
    calls = {(a["tool"], a["outcome"]) for a in map(attrs, points(reader, "jolt.mcp.tool_calls"))}
    assert ("jolt_get_tracks", "ok") in calls
    assert ("jolt_get_source_content", "error") in calls
    ru = [p for p in points(reader, "jolt.cosmos.request_charge") if attrs(p)["surface"] == "mcp"]
    assert {attrs(p)["operation"] for p in ru} >= {"jolt_get_tracks", "jolt_get_source_content"}
    assert all(p.sum == 0 for p in ru)


# --------------------------------------------------------------------------- #
# Domain metrics
# --------------------------------------------------------------------------- #
async def _confirmed_source(runtime, user_id: str, name: str) -> str:
    from jolt.services.uploads import PageSpec, UploadService

    data = name.encode()
    sha = hashlib.sha256(data).hexdigest()
    uploads = UploadService(runtime)
    ticket = await uploads.request_upload(user_id, [PageSpec(name, "image/png", sha)])
    source = await runtime.repos.sources.get_source(ticket.source_id, user_id)
    runtime.blob.put(source.blob_path, data, "image/png")
    await uploads.confirm_upload(user_id, source.id)
    return source.id


async def test_sync_runs_extraction_confidence_and_lease_expiries(reader, runtime, mcp_user):
    uid = mcp_user.user_id
    await mcp_server.build_mcp().call_tool("jolt_sync", {})
    runs = [attrs(p) for p in points(reader, "jolt.sync.runs")]
    assert runs and runs[0]["user_id"] == uid and runs[0]["status"] == "started"
    assert runs[0]["client"] == "Test agent"

    sync = SyncService(runtime)
    source_id = await _confirmed_source(runtime, uid, "a.png")
    await sync.store_extraction(uid, source_id, "# Notes", 0.42, model_id="m-1")
    conf = points(reader, "jolt.extraction.confidence")
    assert len(conf) == 1 and conf[0].sum == pytest.approx(0.42)
    assert attrs(conf[0]) == {"low_confidence": True, "model": "m-1"}

    # A lapsed lease is reclaimed; one that lapsed too often is failed.
    lapsed = await _confirmed_source(runtime, uid, "b.png")
    exhausted = await _confirmed_source(runtime, uid, "c.png")
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    for sid, claims in ((lapsed, 1), (exhausted, runtime.settings.source_max_attempts)):
        source = await runtime.repos.sources.get_source(sid, uid)
        source.processing_status = ProcessingStatus.IN_FLIGHT
        source.lease_expires_at = past
        source.claim_count = claims
        await runtime.repos.sources.replace(source)
    await sync.get_unprocessed_sources(uid)
    expiries = {
        (a["kind"], a["outcome"]) for a in map(attrs, points(reader, "jolt.lease.expiries"))
    }
    assert expiries == {("source", "reclaimed"), ("source", "failed")}
