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
    accounts,
    progress,
    review,
    settings,
    sources,
    tracks,
    upload,
)
from jolt.config import get_settings
from jolt.mcp.context import set_runtime as set_mcp_runtime
from jolt.mcp.server import build_mcp_app
from jolt.runtime import Runtime

logger = logging.getLogger("jolt")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_settings()
    logger.info("Starting jolt-api (env=%s, auth=%s)", cfg.jolt_env, cfg.auth_mode)
    runtime = await Runtime.create(cfg)
    app.state.runtime = runtime
    set_mcp_runtime(runtime)  # share the same gateways with the MCP tools
    try:
        yield
    finally:
        await runtime.close()
        logger.info("Stopped jolt-api")


def create_app() -> FastAPI:
    cfg = get_settings()
    app = FastAPI(title="Jolt API", version="0.2.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # REST (LLD §4 api/routes)
    app.include_router(accounts.router)
    app.include_router(upload.router)
    app.include_router(review.router)
    app.include_router(tracks.router)
    app.include_router(sources.router)
    app.include_router(progress.router)
    app.include_router(settings.router)

    # MCP surface (LLD §4 mcp/) mounted at /mcp
    app.mount("/mcp", build_mcp_app())

    @app.get("/health", tags=["ops"])
    async def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
