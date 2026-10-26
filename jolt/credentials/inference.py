"""Key-backed model calls (LLD §4, §5.1, §11; BE-11).

The only paths that put inference on Jolt's own compute: understanding feedback
and "read now". Strictly opt-in and billed to the user's own key; without a
stored key nothing here makes an outbound call (`resolve_provider` raises
`key_required` first).

Custody rules (LLD §5.1): the plaintext key is read from Key Vault (Managed
Identity) only at the moment of a call, held in a local variable for that call
and dropped; it never goes to disk, into a log line, a span or metric
attribute, an error message or a response body. Errors carry Jolt's own text
(`ModelCallError`), never the provider's.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from jolt import telemetry
from jolt.credentials.providers import (
    KEY_REQUIRED,
    SUPPORTED_PROVIDERS,
    ModelCallError,
    ModelRequest,
    ModelResponse,
    get_adapter,
)
from jolt.runtime import Runtime
from jolt.services.spend import SpendGuard, UsageView

logger = logging.getLogger("jolt.inference")


@dataclass
class ModelCall:
    provider: str
    response: ModelResponse
    usage: UsageView


async def resolve_provider(rt: Runtime, user_id: str, requested: Optional[str] = None) -> str:
    """The provider whose stored key pays for this call: the one asked for, else
    the most recently added key. Raises `key_required` when there is none.
    Reads only Cosmos (the key reference), never Key Vault or the network."""
    refs = await rt.repos.api_keys.list_for_user(user_id)
    refs = [r for r in refs if r.provider in SUPPORTED_PROVIDERS]
    if requested:
        provider = requested.lower()
        if not any(r.provider == provider for r in refs):
            raise ModelCallError(
                KEY_REQUIRED, f"add your {get_adapter(provider).label} API key in Settings first"
            )
        return provider
    if not refs:
        raise ModelCallError(
            KEY_REQUIRED, "this needs your own Anthropic or OpenAI API key; add one in Settings"
        )
    refs.sort(key=lambda r: r.added_at, reverse=True)
    return refs[0].provider


async def call_model(
    rt: Runtime, user_id: str, provider: str, kind: str, request: ModelRequest
) -> ModelCall:
    """One model call on the user's key: spend guard, key from Key Vault, the
    provider call, usage recorded. `kind` (feedback | read_now) labels metrics."""
    adapter = get_adapter(provider)
    guard = SpendGuard(rt)
    await guard.check(user_id)
    try:
        key = await rt.vault.get_key(user_id, adapter.name)
    except Exception as exc:  # missing or unreadable secret: never the value
        raise ModelCallError(
            KEY_REQUIRED, f"the stored {adapter.label} key could not be read; add it again"
        ) from exc
    if not key:
        raise ModelCallError(KEY_REQUIRED, f"add your {adapter.label} API key in Settings first")

    await guard.reserve(user_id)
    outcome = "error"
    try:
        async with httpx.AsyncClient(transport=rt.model_transport) as client:
            response = await adapter.complete(key, request, settings=rt.settings, client=client)
        outcome = "ok"
    except ModelCallError as exc:
        outcome = exc.code
        if key in exc.detail:  # defence in depth; details are Jolt's own text
            exc.detail = exc.detail.replace(key, "[key]")
        raise
    finally:
        del key
        telemetry.record_model_call(provider=adapter.name, kind=kind, outcome=outcome)
        logger.info(
            "model call",
            extra={"provider": adapter.name, "kind": kind, "outcome": outcome},
        )
    usage = await guard.record(user_id, response.input_tokens, response.output_tokens)
    telemetry.record_model_tokens(
        provider=adapter.name,
        kind=kind,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
    )
    return ModelCall(provider=adapter.name, response=response, usage=usage)
