"""Shapes shared by the key-backed routes (BE-11): feedback and read now."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from jolt.services.spend import UsageView

Provider = Literal["anthropic", "openai"]


class ModelUsageResponse(BaseModel):
    input_tokens: int = Field(description="Input tokens of this call (0 when served from cache)")
    output_tokens: int = Field(description="Output tokens of this call (0 when cached)")
    requests_today: int = Field(description="Model requests on your key today (UTC)")
    daily_request_cap: int = Field(description="MODEL_DAILY_REQUEST_CAP")
    tokens_today: int = Field(description="Input + output tokens on your key today (UTC)")
    daily_token_cap: int = Field(description="MODEL_DAILY_TOKEN_CAP")


def usage_response(view: UsageView, input_tokens: int = 0, output_tokens: int = 0):
    return ModelUsageResponse(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        requests_today=view.requests_today,
        daily_request_cap=view.daily_request_cap,
        tokens_today=view.tokens_today,
        daily_token_cap=view.daily_token_cap,
    )
