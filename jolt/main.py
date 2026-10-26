"""FastAPI app assembly, lifespan, mounts (LLD §3, §4).

One deployable unit for the MVP (single-service course correction): the FastAPI
app serves the MCP surface at /mcp and the REST API at /v1. There is no scheduler
and no timed job — FSRS recompute runs inline in the review/grading handlers.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from jolt.api.limits import install_limits
from jolt.api.routes import (
    auth,
    concepts,
    dev_blob,
    mcp_tokens,
    me,
    progress,
    review,
    sources,
    sync,
    tracks,
    upload,
)
from jolt.api.routes import settings as settings_routes
from jolt.api.security import (
    SecurityHeadersMiddleware,
    document_validation_errors,
    validation_error_handler,
)
from jolt.config import Settings, get_settings
from jolt.credentials.providers import ModelCallError
from jolt.mcp.context import set_runtime as set_mcp_runtime
from jolt.mcp.server import build_mcp_app, install_log_redaction
from jolt.runtime import Runtime
from jolt.telemetry import install_telemetry

logger = logging.getLogger("jolt")

# The REST + MCP contract version (contract/openapi.json `info.version`).
API_VERSION = "1.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg: Settings = app.state.settings
    # BE-10: never serve real data with the placeholder token pepper.
    cfg.check_serving_secrets()
    logger.info(
        "Starting jolt-api (env=%s, auth=%s, storage=%s)",
        cfg.jolt_env,
        cfg.auth_mode,
        cfg.storage_mode,
    )
    # A runtime injected by create_app (tests) wins over building one from config.
    runtime = getattr(app.state, "runtime", None) or await Runtime.create(cfg)
    app.state.runtime = runtime
    set_mcp_runtime(runtime)  # share the same gateways with the MCP tools
    if cfg.storage_mode == "memory" and cfg.dsa_seed_from:
        await _seed_curated(runtime, cfg)
    install_log_redaction()  # MCP tokens in ?token= never reach the access log
    try:
        yield
    finally:
        await runtime.close()
        logger.info("Stopped jolt-api")


async def model_call_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """`{detail, code}` with the error's status (and Retry-After when it has one).
    The detail is Jolt's own text: never the provider's, never a key."""
    assert isinstance(exc, ModelCallError)
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
    return JSONResponse(
        {"detail": exc.detail, "code": exc.code}, status_code=exc.status, headers=headers
    )


async def _seed_curated(runtime: Runtime, cfg: Settings) -> None:
    """Memory mode: import the curated DSA track from a Jolt checkout (BE-09)."""
    from jolt.curated.dsa_import import import_dsa
    from jolt.curated.dsa_source import SourceError, load_bundle

    try:
        bundle = load_bundle(cfg.dsa_seed_from, include_new=cfg.dsa_seed_include_new)
        report = await import_dsa(runtime, bundle)
    except (SourceError, ValueError) as exc:
        logger.warning("DSA_SEED_FROM: not imported: %s", exc)
        return
    logger.info(
        "DSA_SEED_FROM: imported %d problems, %d patterns", report.playable, len(bundle.patterns)
    )


def create_app(settings: Settings | None = None, runtime: Runtime | None = None) -> FastAPI:
    """Build the app. `settings` / `runtime` are injection points for tests."""
    cfg = settings or get_settings()
    docs = cfg.docs_enabled
    app = FastAPI(
        title="Jolt API",
        version=API_VERSION,
        lifespan=lifespan,
        # Stable, readable operationIds (the handler name) for generated clients.
        generate_unique_id_function=lambda route: route.name,
        # BE-10: no interactive docs or live schema in prod (API_DOCS_ENABLED);
        # the contract is published in contract/openapi.json.
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    # 422 bodies without the rejected input (it can be a key or an ID token).
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    # BE-11: typed errors of the key-backed routes ({detail, code}).
    app.add_exception_handler(ModelCallError, model_call_error_handler)
    document_validation_errors(app)
    app.state.settings = cfg
    if runtime is not None:
        app.state.runtime = runtime

    # BE-10: rate/size limits, then request metrics (+ Application Insights when
    # configured). Added before CORS so CORS stays outermost and a 429/413
    # still carries CORS headers.
    install_limits(app, cfg)
    install_telemetry(app, cfg)
    # BE-10 security review: nosniff, no framing, no-store on API responses,
    # HSTS in prod (jolt/api/security.py). Outside the limits, so a 429 has them.
    app.add_middleware(SecurityHeadersMiddleware, settings=cfg)

    # CORS for the browser app (BE-02), from CORS_ORIGINS / CORS_ORIGIN_REGEX.
    # Applied app-wide so it covers /v1 and, in memory mode, the /_dev/blob
    # upload URLs; non-browser clients (MCP agents) ignore it. The token rides
    # the Authorization header, never a cookie, so credentials stay off.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origin_list,
        allow_origin_regex=cfg.cors_origin_regex or None,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["Retry-After"],
        max_age=600,
    )

    # REST (LLD §4 api/routes)
    app.include_router(auth.router)
    if cfg.dev_auth_enabled:
        # Dev-only sign-in by email (BE-03); Settings refuses it in prod.
        app.include_router(auth.dev_router)
    app.include_router(me.router)
    app.include_router(mcp_tokens.router)
    app.include_router(upload.router)
    app.include_router(review.router)
    app.include_router(review.sessions_router)
    app.include_router(concepts.router)
    app.include_router(tracks.router)
    app.include_router(sources.router)
    app.include_router(sources.storage_router)
    app.include_router(progress.router)
    app.include_router(settings_routes.router)
    app.include_router(sync.router)
    if cfg.storage_mode == "memory":
        # Serves the in-memory blob fake's upload/read URLs (dev only).
        app.include_router(dev_blob.router)

    # MCP surface (LLD §4 mcp/) mounted at /mcp
    app.mount("/mcp", build_mcp_app())

    @app.get("/health", tags=["ops"])
    async def health() -> dict:
        # `version` lets the deploy workflow wait for the new revision.
        return {"status": "ok", "version": cfg.app_version}

    return app


app = create_app()
