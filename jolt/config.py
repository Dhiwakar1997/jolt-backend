"""Application settings, loaded from environment / `.env` via pydantic-settings.

Per LLD §9: config comes from the environment; Container Apps injects secrets as
env references to Key Vault. Locally, a git-ignored `.env` supplies the same
variables. Nothing here is a secret at rest — it is the map of where secrets live.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- General ----
    jolt_env: Literal["dev", "prod"] = "dev"
    auth_mode: Literal["managed_identity", "key"] = "key"
    # Browser origins allowed to call the API (comma-separated exact origins, or
    # `*`). The app sends its token in the Authorization header, never cookies,
    # so credentials are not allowed cross-origin.
    cors_origins: str = "http://localhost:5173"
    # Optional regex for origins that cannot be listed exactly, e.g. Netlify
    # deploy previews: ^https://deploy-preview-\d+--jolt\.netlify\.app$
    cors_origin_regex: str = ""
    # Build identifier reported by /health (the image sets it to the git SHA).
    app_version: str = "dev"
    # Public base URL of this server. Used in memory mode only: when set, the
    # in-memory blob fake mints upload/read URLs served by this app at
    # /_dev/blob/... so a browser or scripts/smoke.py can PUT/GET for real.
    # Empty -> opaque memory:// URLs that nothing serves (tests).
    public_base_url: str = ""
    # Where state lives: `azure` (Cosmos/Blob/Key Vault) or `memory` (in-process
    # fakes from jolt/data/memory.py — tests and cloud-free local runs only;
    # everything is lost on restart).
    storage_mode: Literal["azure", "memory"] = "azure"

    # ---- Auth (BE-03) ----
    # Two opaque token kinds on one hashing scheme (Argon2 + pepper, plus an
    # HMAC lookup index keyed by the pepper): app tokens for /v1, MCP tokens for
    # /mcp. Keep the prefixes stable once tokens exist.
    app_token_prefix: str = "jolt_app_"
    mcp_token_prefix: str = "jolt_live_"
    token_hash_pepper: str = "change-me-to-a-long-random-string"
    # Google sign-in: the OAuth client id(s) an ID token must be issued to (its
    # `aud`). Comma-separated, so a web and a mobile client can both sign in.
    # Empty -> POST /v1/auth/google answers 503 (not configured).
    google_oauth_client_id: str = ""
    # The MCP address the app shows on "Connect your model", e.g.
    # https://<api-host>/mcp/sse. Empty -> derived from PUBLIC_BASE_URL, else
    # from the request's own base URL.
    mcp_server_url: str = ""
    # Dev-only sign-in without Google: POST /v1/auth/dev {email}. Never in prod.
    # With STORAGE_MODE=azure it also needs DEV_AUTH_SECRET, sent by the caller
    # as the `X-Jolt-Dev-Auth-Secret` header (the deployed smoke test).
    dev_auth_enabled: bool = False
    dev_auth_secret: str = ""

    # ---- Cosmos ----
    cosmos_endpoint: str = ""
    cosmos_database: str = "jolt"
    cosmos_key: str = ""

    # ---- Blob ----
    blob_account_url: str = ""
    blob_container: str = "sources"
    blob_connection_string: str = ""
    blob_account_key: str = ""
    blob_sas_ttl_seconds: int = 900

    # ---- Key Vault ----
    keyvault_url: str = ""

    # ---- Leases ----
    lease_ttl_seconds: int = 900

    # ---- Nightly sync (BE-04) ----
    # A running sync run with no tool call for this long is closed as
    # `incomplete` (the client stopped before its final jolt_sync).
    sync_run_idle_seconds: int = 1800
    # GET /v1/sync/status reports `late` when the last run started longer ago
    # than this (a daily schedule plus slack for a missed or slow night).
    sync_late_after_hours: int = 30
    # Recommended local time for the scheduled task (decision D5).
    sync_schedule_time: str = "00:15"
    # Accept the MCP token as `?token=` on GET /mcp/sse, for clients whose
    # connector settings take only a URL (Claude custom connectors). The token
    # is redacted from access logs. See docs/CONNECT.md.
    mcp_query_token_enabled: bool = True

    # ---- FSRS ----
    fsrs_desired_retention: float = Field(default=0.9, ge=0.70, le=0.97)

    # ---- Key-backed providers (dormant) ----
    anthropic_api_base: str = "https://api.anthropic.com"
    openai_api_base: str = "https://api.openai.com"

    @model_validator(mode="after")
    def _dev_auth_is_safe(self) -> "Settings":
        if not self.dev_auth_enabled:
            return self
        if self.jolt_env == "prod":
            raise ValueError("DEV_AUTH_ENABLED must not be set when JOLT_ENV=prod")
        if self.storage_mode == "azure" and not self.dev_auth_secret:
            raise ValueError(
                "DEV_AUTH_ENABLED with STORAGE_MODE=azure needs DEV_AUTH_SECRET "
                "(a public dev server must not mint accounts for anyone)"
            )
        return self

    @property
    def google_client_ids(self) -> list[str]:
        return [c.strip() for c in self.google_oauth_client_id.split(",") if c.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def use_managed_identity(self) -> bool:
        return self.auth_mode == "managed_identity"


@lru_cache
def get_settings() -> Settings:
    """Cached singleton so env is parsed once per process."""
    return Settings()
