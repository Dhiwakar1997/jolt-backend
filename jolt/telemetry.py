"""Observability (BE-10): OpenTelemetry traces, logs and metrics to Application Insights.

Off unless `APPLICATIONINSIGHTS_CONNECTION_STRING` is set: then
`configure_telemetry` loads the Azure Monitor OpenTelemetry distro once per
process, which exports the FastAPI request spans (the App Insights `requests`
table), outgoing HTTP dependencies (Cosmos, Blob, Key Vault, Google), the
`jolt.*` loggers, and the custom metrics below. Without it the OpenTelemetry API
is a no-op: every `record_*` call here is cheap and safe, and tests need nothing.

Custom metrics (App Insights `customMetrics`, attributes in `customDimensions`;
infra/workbook.json charts them):

* `jolt.sync.runs` (counter): `user_id`, `status` (started / finished /
  incomplete), `client`.
* `jolt.extraction.confidence` (histogram, 0..1): `low_confidence`, `model`.
* `jolt.lease.expiries` (counter): `kind` (source), `outcome` (reclaimed / failed).
* `jolt.cosmos.request_charge` (histogram, RU per REST request or tool call):
  `surface` (rest / mcp), `operation` (`<METHOD> <route template>` / tool name).
* `jolt.http.requests` (counter): `route`, `method`, `status_code`,
  `status_class` (2xx..5xx).
* `jolt.mcp.tool_calls` (counter): `tool`, `outcome` (ok / error /
  unauthorized / rate_limited).
* `jolt.rate_limited` (counter): `surface` (rest / mcp), `scope` (token / ip).
* `jolt.maintenance.deleted` (counter): `kind` (abandoned_upload /
  expired_source / blob / expired_app_token).

Cosmos RU: every Cosmos HTTP response carries `x-ms-request-charge`. The
Cosmos client is built with `raw_response_hook=cosmos_response_hook`
(jolt/data/cosmos.py), which adds the charge to the meter of the current
request or tool call (`metering()`, a contextvar). The in-memory fakes make no
HTTP calls, so in memory mode every request records 0 RU.

`user_id` is Jolt's opaque id (a hash of the sign-in identity), never an email.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator, Optional

from opentelemetry import metrics

from jolt.config import Settings

logger = logging.getLogger("jolt.telemetry")

SERVICE_NAME = "jolt-api"
_configured = False


def configure_telemetry(settings: Settings, *, service_name: str = SERVICE_NAME) -> bool:
    """Start exporting to Application Insights if a connection string is set.

    Idempotent (the distro installs process-global providers). Returns whether
    telemetry is on. Must run before the FastAPI app is built, so the distro's
    FastAPI instrumentation wraps it.
    """
    global _configured
    conn = settings.applicationinsights_connection_string.strip()
    if not conn:
        return False
    if _configured:
        return True
    # Health probes would dominate the request table; leave them out.
    os.environ.setdefault("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", "health")
    os.environ.setdefault("OTEL_SERVICE_NAME", service_name)
    from azure.monitor.opentelemetry import configure_azure_monitor
    from opentelemetry.sdk.resources import Resource

    configure_azure_monitor(
        connection_string=conn,
        # Only Jolt's own loggers: never the access log (it can carry ?token=,
        # which is redacted there, but there is no reason to ship it). Request
        # spans get the same redaction (instrument_app).
        logger_name="jolt",
        resource=Resource.create(
            {
                "service.name": service_name,
                "service.version": settings.app_version,
                "deployment.environment": settings.jolt_env,
            }
        ),
        enable_live_metrics=False,
        # install_telemetry instruments the app it is given; the distro's
        # global FastAPI patch would only catch apps built after this call.
        instrumentation_options={"fastapi": {"enabled": False}},
    )
    _configured = True
    logger.info("telemetry: exporting to Application Insights")
    return True


def is_enabled() -> bool:
    return _configured


# --------------------------------------------------------------------------- #
# Instruments
# --------------------------------------------------------------------------- #
class _Instruments:
    def __init__(self, meter: metrics.Meter) -> None:
        self.sync_runs = meter.create_counter(
            "jolt.sync.runs", unit="1", description="Nightly sync runs, by user and outcome"
        )
        self.extraction_confidence = meter.create_histogram(
            "jolt.extraction.confidence", unit="1", description="Confidence of stored reads"
        )
        self.lease_expiries = meter.create_counter(
            "jolt.lease.expiries", unit="1", description="Sync leases that lapsed"
        )
        self.cosmos_charge = meter.create_histogram(
            "jolt.cosmos.request_charge",
            unit="RU",
            description="Cosmos request units per REST request or MCP tool call",
        )
        self.http_requests = meter.create_counter(
            "jolt.http.requests", unit="1", description="HTTP requests by route and status"
        )
        self.tool_calls = meter.create_counter(
            "jolt.mcp.tool_calls", unit="1", description="MCP tool calls by tool and outcome"
        )
        self.rate_limited = meter.create_counter(
            "jolt.rate_limited", unit="1", description="Requests refused with 429"
        )
        self.maintenance_deleted = meter.create_counter(
            "jolt.maintenance.deleted", unit="1", description="Items removed by maintenance"
        )


# The global meter is a proxy until a provider is installed, so instruments
# made now start exporting once configure_telemetry runs.
_instruments = _Instruments(metrics.get_meter("jolt"))


def bind_meter(meter: metrics.Meter) -> None:
    """Record on `meter` instead (tests read metrics with an in-memory reader)."""
    global _instruments
    _instruments = _Instruments(meter)


def reset_meter() -> None:
    bind_meter(metrics.get_meter("jolt"))


def _safe(fn):
    """Telemetry never breaks a request."""

    def wrapper(*args, **kwargs):
        try:
            fn(*args, **kwargs)
        except Exception:  # pragma: no cover - defensive
            logger.debug("telemetry: %s failed", fn.__name__, exc_info=True)

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


@_safe
def record_sync_run(user_id: str, status: str, client: Optional[str] = None) -> None:
    _instruments.sync_runs.add(
        1, {"user_id": user_id, "status": status, "client": client or "unknown"}
    )


@_safe
def record_extraction(confidence: float, *, low: bool, model: Optional[str]) -> None:
    _instruments.extraction_confidence.record(
        float(confidence), {"low_confidence": low, "model": model or "unknown"}
    )
    # One log line per read: the workbook's confidence distribution reads these
    # (`traces`), since an exported histogram keeps only per-interval sums.
    logger.info(
        "extraction stored",
        extra={"confidence": float(confidence), "low_confidence": low, "model": model or ""},
    )


@_safe
def record_lease_expiry(kind: str, outcome: str) -> None:
    _instruments.lease_expiries.add(1, {"kind": kind, "outcome": outcome})


@_safe
def record_request(*, route: str, method: str, status_code: int, request_charge: float) -> None:
    attrs = {
        "route": route,
        "method": method,
        "status_code": status_code,
        "status_class": f"{status_code // 100}xx",
    }
    _instruments.http_requests.add(1, attrs)
    _instruments.cosmos_charge.record(
        request_charge, {"surface": "rest", "operation": f"{method} {route}"}
    )


@_safe
def record_tool_call(tool: str, outcome: str, request_charge: float) -> None:
    _instruments.tool_calls.add(1, {"tool": tool, "outcome": outcome})
    _instruments.cosmos_charge.record(request_charge, {"surface": "mcp", "operation": tool})


@_safe
def record_rate_limited(surface: str, scope: str) -> None:
    _instruments.rate_limited.add(1, {"surface": surface, "scope": scope})


@_safe
def record_maintenance(kind: str, count: int) -> None:
    if count:
        _instruments.maintenance_deleted.add(count, {"kind": kind})


# --------------------------------------------------------------------------- #
# Cosmos request charge
# --------------------------------------------------------------------------- #
@dataclass
class RequestCharge:
    """RU spent by one REST request or MCP tool call."""

    total: float = 0.0
    requests: int = 0


_charge: ContextVar[Optional[RequestCharge]] = ContextVar("jolt_cosmos_charge", default=None)


@contextmanager
def metering() -> Iterator[RequestCharge]:
    """Collect the Cosmos RU of everything awaited inside the block."""
    meter = RequestCharge()
    token = _charge.set(meter)
    try:
        yield meter
    finally:
        _charge.reset(token)


def add_request_charge(value: float) -> None:
    meter = _charge.get()
    if meter is not None:
        meter.total += value
        meter.requests += 1


def cosmos_response_hook(response: Any) -> None:
    """azure-core `raw_response_hook`: read `x-ms-request-charge` off a Cosmos
    HTTP response (a PipelineResponse) and add it to the current meter."""
    try:
        raw = response.http_response.headers.get("x-ms-request-charge")
        if raw:
            add_request_charge(float(raw))
    except Exception:  # pragma: no cover - never fail a Cosmos call over metrics
        logger.debug("telemetry: unreadable request charge", exc_info=True)


# --------------------------------------------------------------------------- #
# REST request metrics
# --------------------------------------------------------------------------- #
def route_template(scope: dict) -> str:
    """The matched route's template (`/v1/sources/{source_id}`), so metrics do
    not get one series per id. The MCP mount reports its sub-path."""
    route = scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return path
    raw = scope.get("path", "")
    if raw.startswith("/mcp/messages"):
        return "/mcp/messages/"
    if raw.startswith("/mcp"):
        return "/mcp/sse" if raw.rstrip("/").endswith("/sse") else "/mcp"
    if raw.startswith("/_dev/blob"):
        return "/_dev/blob"
    return "unmatched"


class RequestMetricsMiddleware:
    """Pure ASGI (streams pass through untouched): counts every request by
    route and status and records the Cosmos RU it spent. `/health` is skipped."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return
        status = 500

        async def send_wrapper(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        with metering() as charge:
            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                record_request(
                    route=route_template(scope),
                    method=scope.get("method", "GET"),
                    status_code=status,
                    request_charge=charge.total,
                )


def install_telemetry(app, settings: Settings) -> bool:
    """Request metrics on `app`, plus export when configured. Call it before the
    CORS middleware is added so CORS stays outermost."""
    enabled = configure_telemetry(settings)
    if enabled:
        try:
            instrument_app(app)
        except Exception:  # pragma: no cover - the distro may already have done it
            logger.debug("telemetry: FastAPI instrumentation skipped", exc_info=True)
    app.add_middleware(RequestMetricsMiddleware)
    return enabled


def instrument_app(app, *, tracer_provider: Any = None) -> None:
    """Request spans (the App Insights `requests` table) for `app`.

    `redact_span` blanks `?token=` (an MCP token) and `session_id=` in the
    span's URL attributes before export: the instrumentation records the full
    URL, query string included (BE-10 security review, docs/SECURITY.md).
    """
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    from jolt.api.security import redact_span

    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls="health",
        server_request_hook=redact_span,
        tracer_provider=tracer_provider,
    )


def flush() -> None:
    """Export what is buffered (short-lived processes: the maintenance job)."""
    if not _configured:
        return
    from opentelemetry import trace
    from opentelemetry._logs import get_logger_provider

    providers = (metrics.get_meter_provider(), trace.get_tracer_provider(), get_logger_provider())
    for provider in providers:
        force_flush = getattr(provider, "force_flush", None)
        if force_flush is not None:
            try:
                force_flush()
            except Exception:  # pragma: no cover - best effort
                logger.debug("telemetry: flush failed", exc_info=True)
