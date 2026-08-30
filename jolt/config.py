"""Application settings, loaded from environment / `.env` via pydantic-settings.

Per LLD §9: config comes from the environment; Container Apps injects secrets as
env references to Key Vault. Locally, a git-ignored `.env` supplies the same
variables. Nothing here is a secret at rest — it is the map of where secrets live.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
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
    cors_origins: str = "*"

    # ---- Auth ----
    token_prefix: str = "jolt_live_"
    token_hash_pepper: str = "change-me-to-a-long-random-string"

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

    # ---- FSRS ----
    fsrs_desired_retention: float = Field(default=0.9, ge=0.70, le=0.97)

    # ---- Key-backed providers (dormant) ----
    anthropic_api_base: str = "https://api.anthropic.com"
    openai_api_base: str = "https://api.openai.com"

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
