"""Settings routes — the Settings screen, data retention and per-user API keys
(LLD §5.1, §7.6; BE-06, BE-08).

`GET` / `PATCH /v1/settings` carry every preference on the Settings screen
(appearance, reviews, data, time zone) plus, on the way out, the model status
block (MCP tokens with last use, last sync run and health). Review settings
take effect on the next review session: desired retention feeds every FSRS
fold of the user's cards, the daily cap bounds the next session's queue.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator, model_validator

from jolt.api.deps import RuntimeDep, UserDep
from jolt.api.errors import BAD_REQUEST, UNAUTHORIZED
from jolt.api.routes.mcp_tokens import McpTokenResponse
from jolt.api.routes.mcp_tokens import _out as token_out
from jolt.api.routes.me import mcp_server_url
from jolt.api.routes.sync import SyncRunResponse, run_response
from jolt.domain.models import (
    DESIRED_RETENTION_MAX,
    DESIRED_RETENTION_MIN,
    Theme,
    User,
    UserSettings,
)
from jolt.domain.sync import next_expected, parse_schedule_time, sync_health
from jolt.domain.zones import is_valid_zone, user_zone
from jolt.runtime import Runtime
from jolt.services.accounts import AccountService
from jolt.services.credentials import CredentialError, CredentialService
from jolt.services.sync_runs import SyncRunService

router = APIRouter(prefix="/v1/settings", tags=["settings"], responses=UNAUTHORIZED)

# Same ceiling as one review session's `limit`.
DAILY_REVIEW_CAP_MAX = 500
DATA_RETENTION_DAYS_MAX = 3650


class ApiKeyRequest(BaseModel):
    provider: str
    key: str


class ApiKeyResponse(BaseModel):
    provider: str
    last4: str
    status: str
    added_at: Optional[datetime] = Field(
        default=None, description="When this key was stored (replacing a key resets it)"
    )


class ModelStatusResponse(BaseModel):
    mcp_server_url: str = Field(description="The address to paste into Claude / Codex")
    tokens: list[McpTokenResponse] = Field(
        description="Connected clients (MCP tokens), newest first, with last use"
    )
    last_sync: Optional[SyncRunResponse] = Field(description="The latest sync run, if any")
    health: Literal["ok", "late", "never"] = Field(description="As on GET /v1/sync/status")
    next_expected_at: Optional[datetime] = Field(
        description="The next scheduled sync (SYNC_SCHEDULE_TIME in the user's time_zone), UTC"
    )


class SettingsFields(BaseModel):
    theme: Theme = Field(description="light | dark | system")
    desired_retention: float = Field(
        description=f"FSRS desired retention, {DESIRED_RETENTION_MIN:.2f}-"
        f"{DESIRED_RETENTION_MAX:.2f}: the chance of still remembering a concept when it "
        "comes due. Higher means shorter intervals and more reviews"
    )
    retire_mastered: bool = Field(description="Mastered concepts leave the review queue")
    allow_question_rewrite: bool = Field(
        description="Let the model rewrite a question after grading (decision D8, opt-in)"
    )
    daily_review_cap: Optional[int] = Field(
        description="Most answers a day; review sessions are cut to what is left. null: no cap"
    )
    data_retention_days: Optional[int] = Field(
        description="How long to keep uploads, in days (stored preference; not enforced yet). "
        "null: keep everything. Not FSRS retention: see desired_retention"
    )
    time_zone: str = Field(
        description="IANA zone of the learner's day (today, streak, heatmap, upload dates)"
    )


class SettingsResponse(SettingsFields):
    model_status: ModelStatusResponse


class SettingsPatch(BaseModel):
    """Only the fields sent change. `null` resets `desired_retention` to the server
    default and clears `daily_review_cap` / `data_retention_days`."""

    theme: Optional[Theme] = None
    desired_retention: Optional[float] = Field(
        default=None, ge=DESIRED_RETENTION_MIN, le=DESIRED_RETENTION_MAX
    )
    retire_mastered: Optional[bool] = None
    allow_question_rewrite: Optional[bool] = None
    daily_review_cap: Optional[int] = Field(default=None, ge=1, le=DAILY_REVIEW_CAP_MAX)
    data_retention_days: Optional[int] = Field(default=None, ge=1, le=DATA_RETENTION_DAYS_MAX)
    time_zone: Optional[str] = Field(default=None, min_length=1, max_length=64)

    @field_validator("time_zone")
    @classmethod
    def _known_zone(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not is_valid_zone(v):
            raise ValueError(f"unknown time zone: {v}")
        return v

    @model_validator(mode="after")
    def _no_null_for_required(self) -> "SettingsPatch":
        nullable = {"desired_retention", "daily_review_cap", "data_retention_days"}
        for name in self.model_fields_set - nullable:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self

    def changes(self) -> dict:
        return {name: getattr(self, name) for name in self.model_fields_set}


def settings_fields(settings: UserSettings, rt: Runtime) -> SettingsFields:
    return SettingsFields(
        theme=settings.theme,
        desired_retention=settings.desired_retention or rt.settings.fsrs_desired_retention,
        retire_mastered=settings.retire_mastered,
        allow_question_rewrite=settings.allow_question_rewrite,
        daily_review_cap=settings.daily_review_cap,
        data_retention_days=settings.data_retention_days,
        time_zone=settings.time_zone,
    )


async def model_status(user: User, rt: Runtime, request: Request) -> ModelStatusResponse:
    tokens = await AccountService(rt).list_mcp_tokens(user.id)
    runs = await SyncRunService(rt).recent(user.id, 1)
    last = runs[0] if runs else None
    now = datetime.now(timezone.utc)
    return ModelStatusResponse(
        mcp_server_url=mcp_server_url(rt.settings, request),
        tokens=[token_out(t) for t in tokens],
        last_sync=run_response(last) if last else None,
        health=sync_health(
            last.started_at if last else None,
            now,
            timedelta(hours=rt.settings.sync_late_after_hours),
        ),
        next_expected_at=next_expected(
            now, parse_schedule_time(rt.settings.sync_schedule_time), user_zone(user.settings)
        ),
    )


async def _settings_view(user: User, rt: Runtime, request: Request) -> SettingsResponse:
    return SettingsResponse(
        **settings_fields(user.settings, rt).model_dump(),
        model_status=await model_status(user, rt, request),
    )


@router.get("", response_model=SettingsResponse)
async def get_settings(user: UserDep, rt: RuntimeDep, request: Request) -> SettingsResponse:
    current = await rt.repos.users.get_user(user.user_id) or user.user
    return await _settings_view(current, rt, request)


async def _update(rt: Runtime, user: User, changes: dict) -> User:
    """Etag-guarded read-modify-write of the user's settings."""
    for _ in range(5):
        current = await rt.repos.users.get_user(user.id) or user
        current.settings = current.settings.model_copy(update=changes)
        if not changes:
            return current
        saved = await rt.repos.users.replace(current, etag=current.etag)
        if saved is not None:
            return saved
    raise HTTPException(status_code=409, detail="settings changed concurrently; try again")


@router.patch("", response_model=SettingsResponse, responses={409: {"description": "Conflict"}})
async def update_settings(
    body: SettingsPatch, user: UserDep, rt: RuntimeDep, request: Request
) -> SettingsResponse:
    saved = await _update(rt, user.user, body.changes())
    return await _settings_view(saved, rt, request)


@router.post(
    "/api-key",
    response_model=ApiKeyResponse,
    responses={
        **BAD_REQUEST,
        # Same status as request validation, but with a string `detail`.
        422: {"description": "Validation error, or the provider rejected the key"},
    },
)
async def set_api_key(body: ApiKeyRequest, user: UserDep, rt: RuntimeDep) -> ApiKeyResponse:
    try:
        stored = await CredentialService(rt).capture(user.user_id, body.provider, body.key)
    except ValueError as exc:  # unsupported provider
        raise HTTPException(status_code=400, detail=str(exc))
    except CredentialError as exc:  # failed validation
        raise HTTPException(status_code=422, detail=str(exc))
    return ApiKeyResponse(**stored.__dict__)


@router.get("/api-key", response_model=list[ApiKeyResponse])
async def list_api_keys(user: UserDep, rt: RuntimeDep) -> list[ApiKeyResponse]:
    """Stored provider keys, newest first: provider, last four characters, when added."""
    keys = await CredentialService(rt).list_keys(user.user_id)
    return [ApiKeyResponse(**k.__dict__) for k in keys]


@router.delete("/api-key/{provider}", status_code=204, responses=BAD_REQUEST)
async def delete_api_key(provider: str, user: UserDep, rt: RuntimeDep) -> Response:
    try:
        await CredentialService(rt).delete(user.user_id, provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return Response(status_code=204)


class RetentionRequest(BaseModel):
    retention_days: int | None = Field(default=None, ge=1, le=DATA_RETENTION_DAYS_MAX)


class RetentionResponse(BaseModel):
    retention_days: int | None


@router.put("/retention", response_model=RetentionResponse, deprecated=True)
async def set_retention(body: RetentionRequest, user: UserDep, rt: RuntimeDep) -> RetentionResponse:
    """Deprecated: `PATCH /v1/settings {data_retention_days}` (the same value).

    Data retention in days, not FSRS desired retention."""
    saved = await _update(rt, user.user, {"data_retention_days": body.retention_days})
    return RetentionResponse(retention_days=saved.settings.data_retention_days)
