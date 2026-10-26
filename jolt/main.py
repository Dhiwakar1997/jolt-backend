"""FastAPI app assembly, lifespan, mounts (LLD §3, §4).

One deployable unit for the MVP (single-service course correction): the FastAPI
app serves the MCP surface at /mcp and the REST API at /v1. There is no scheduler
and no timed job — FSRS recompute runs inline in the review/grading handlers.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
from jolt.config import Settings, get_settings
from jolt.mcp.context import set_runtime as set_mcp_runtime
from jolt.mcp.server import build_mcp_app, install_log_redaction
from jolt.runtime import Runtime

logger = logging.getLogger("jolt")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg: Settings = app.state.settings
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
    install_log_redaction()  # MCP tokens in ?token= never reach the access log
    try:
        yield
    finally:
        await runtime.close()
        logger.info("Stopped jolt-api")


def create_app(settings: Settings | None = None, runtime: Runtime | None = None) -> FastAPI:
    """Build the app. `settings` / `runtime` are injection points for tests."""
    cfg = settings or get_settings()
    app = FastAPI(
        title="Jolt API",
        version="0.7.0",
        lifespan=lifespan,
        # Stable, readable operationIds (the handler name) for generated clients.
        generate_unique_id_function=lambda route: route.name,
    )
    app.state.settings = cfg
    if runtime is not None:
        app.state.runtime = runtime

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
