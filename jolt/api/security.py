"""HTTP hardening from the BE-10 security review (docs/SECURITY.md).

* **Secret redaction in URLs.** Two query parameters are capabilities: `token`
  (an MCP token on `GET /mcp/sse?token=`, for clients that take only a URL) and
  `session_id` (`POST /mcp/messages/?session_id=`, which routes a message into
  an open MCP session). `redact_secrets` blanks both; the Uvicorn access log
  (jolt/mcp/server.py) and the OpenTelemetry request spans sent to Application
  Insights (jolt/telemetry.py, `redact_span`) use it.
* **Security headers** on every response (`SecurityHeadersMiddleware`): no
  MIME sniffing, no framing, no referrer, HSTS in prod, and on API responses
  `Cache-Control: no-store` (they carry tokens and personal data) and a
  `default-src 'none'` CSP (JSON is never a document). The interactive docs
  pages (`/docs`, `/redoc`, dev only) keep their own scripts, so they get no CSP.
* **Validation errors without echoes** (`validation_error_handler`): FastAPI's
  422 body repeats the rejected `input`, which for `POST /v1/settings/api-key`
  or `POST /v1/auth/google` can be a provider API key or an ID token. The
  handler keeps `loc`, `msg`, `type` (and `ctx`) and drops `input`.
"""

from __future__ import annotations

import re
from typing import Any, Iterable
from urllib.parse import parse_qsl

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from jolt.config import Settings

# Query parameters whose values are secrets (see the module docstring).
SECRET_QUERY_PARAMS = ("token", "session_id")
REDACTED = "[redacted]"

# `token=<value>` / `session_id=<value>` after `?` or `&`, in a URL or a log line.
_SECRET_IN_URL = re.compile(r"(?i)([?&](?:token|session_id)=)[^&\s\"'#]+")


def redact_secrets(text: str) -> str:
    """Blank the value of every secret query parameter in `text`."""
    return _SECRET_IN_URL.sub(r"\1" + REDACTED, text)


def redact_query(query: str) -> str:
    """A raw query string (no `?`) with secret values blanked."""
    if not query:
        return query
    return redact_secrets("?" + query)[1:]


def has_secret_query(query: str) -> bool:
    try:
        return any(k.lower() in SECRET_QUERY_PARAMS for k, _ in parse_qsl(query))
    except ValueError:  # pragma: no cover - parse_qsl is lenient
        return True


# Span attributes (old and new HTTP semantic conventions) that can carry the
# request's query string.
_URL_ATTRIBUTES = ("http.url", "url.full", "http.target", "url.query")


def redact_span(span: Any, scope: dict) -> None:
    """OpenTelemetry `server_request_hook`: blank secret query values in the
    URL attributes of the request span before it is exported. Runs for every
    request; cheap when the query has nothing to hide."""
    if span is None or not getattr(span, "is_recording", lambda: False)():
        return
    raw = scope.get("query_string", b"")
    query = raw.decode("latin-1") if isinstance(raw, bytes) else str(raw or "")
    if not query or not has_secret_query(query):
        return
    attributes = getattr(span, "attributes", None) or {}
    for name in _URL_ATTRIBUTES:
        value = attributes.get(name)
        if isinstance(value, str) and value:
            redact = redact_query if name == "url.query" else redact_secrets
            span.set_attribute(name, redact(value))


# --------------------------------------------------------------------------- #
# Security headers
# --------------------------------------------------------------------------- #
_DOCS_PATHS = ("/docs", "/redoc")


def security_headers(path: str, *, hsts: bool) -> list[tuple[bytes, bytes]]:
    headers = [
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"no-referrer"),
        (b"cross-origin-resource-policy", b"same-site"),
    ]
    if hsts:
        headers.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
    if not path.startswith(_DOCS_PATHS):
        headers.append((b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"))
    if path.startswith("/v1") or path.startswith("/mcp"):
        headers.append((b"cache-control", b"no-store"))
    return headers


class SecurityHeadersMiddleware:
    """Pure ASGI: adds `security_headers` to every HTTP response start, without
    overriding a header the app already set. Streams (MCP SSE) pass through."""

    def __init__(self, app, settings: Settings) -> None:
        self.app = app
        self.hsts = settings.jolt_env == "prod"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        extra = security_headers(scope.get("path", ""), hsts=self.hsts)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                present = {k.lower() for k, _ in message.get("headers", [])}
                headers = list(message.get("headers", []))
                headers += [(k, v) for k, v in extra if k not in present]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


# --------------------------------------------------------------------------- #
# 422 without the rejected input
# --------------------------------------------------------------------------- #
def scrub_errors(errors: Iterable[dict]) -> list[dict]:
    return [{k: v for k, v in e.items() if k not in ("input", "url")} for e in errors]


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's 422 body (`{"detail": [{loc, msg, type, ctx?}]}`) minus the
    `input` each error echoes, which may be a secret the caller just sent."""
    return JSONResponse(
        status_code=422, content={"detail": jsonable_encoder(scrub_errors(exc.errors()))}
    )


def document_validation_errors(app) -> None:
    """The OpenAPI `ValidationError` schema without `input`, which
    `validation_error_handler` never sends."""
    original = app.openapi

    def openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = original()
        error = schema.get("components", {}).get("schemas", {}).get("ValidationError", {})
        error.get("properties", {}).pop("input", None)
        app.openapi_schema = schema
        return schema

    app.openapi = openapi
