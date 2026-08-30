"""Provider-agnostic adapter for per-user API keys (LLD §5.1).

One interface — `validate` now, `call` later — normalises the differences between
providers. Adding a provider is a new adapter, not a change to callers. MVP ships
validation for Anthropic and OpenAI; the call path is dormant (inference.py).

Validation is a cheap, unbilled probe (a minimal models-list call), so a malformed
or unauthorised key is rejected at capture rather than discovered at first use.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass

import httpx

from jolt.config import get_settings


@dataclass
class ValidationResult:
    ok: bool
    last4: str
    detail: str = ""


class ProviderAdapter(abc.ABC):
    name: str

    @abc.abstractmethod
    async def validate(self, key: str) -> ValidationResult: ...

    @staticmethod
    def _last4(key: str) -> str:
        return key[-4:] if len(key) >= 4 else key


class AnthropicAdapter(ProviderAdapter):
    name = "anthropic"

    async def validate(self, key: str) -> ValidationResult:
        base = get_settings().anthropic_api_base.rstrip("/")
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{base}/v1/models", headers=headers)
        ok = resp.status_code == 200
        return ValidationResult(
            ok=ok,
            last4=self._last4(key),
            detail="" if ok else f"provider returned {resp.status_code}",
        )


class OpenAIAdapter(ProviderAdapter):
    name = "openai"

    async def validate(self, key: str) -> ValidationResult:
        base = get_settings().openai_api_base.rstrip("/")
        headers = {"Authorization": f"Bearer {key}"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{base}/v1/models", headers=headers)
        ok = resp.status_code == 200
        return ValidationResult(
            ok=ok,
            last4=self._last4(key),
            detail="" if ok else f"provider returned {resp.status_code}",
        )


_ADAPTERS: dict[str, ProviderAdapter] = {
    AnthropicAdapter.name: AnthropicAdapter(),
    OpenAIAdapter.name: OpenAIAdapter(),
}

SUPPORTED_PROVIDERS = tuple(_ADAPTERS.keys())


def get_adapter(provider: str) -> ProviderAdapter:
    try:
        return _ADAPTERS[provider.lower()]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported provider '{provider}'. Supported: {', '.join(SUPPORTED_PROVIDERS)}"
        ) from exc
