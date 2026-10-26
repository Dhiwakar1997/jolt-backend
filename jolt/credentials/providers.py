"""Provider adapters for per-user API keys (LLD §5.1; BE-11).

One interface normalises the providers: `validate` (a cheap, unbilled
models-list probe, so a bad key is rejected at capture) and `complete` (one
model call billed to the user's own key). Adding a provider is a new adapter,
not a change to callers.

* **Anthropic**: the Messages API (`POST {ANTHROPIC_API_BASE}/v1/messages`).
  Images go in as `image` blocks and PDFs as `document` blocks, base64.
* **OpenAI**: the Responses API (`POST {OPENAI_API_BASE}/v1/responses`), with
  `store: false`. Images go in as `input_image` and PDFs as `input_file`, both
  as base64 data URLs.

Calls are plain HTTPS through `httpx` (already a dependency): one transport for
both providers, so tests swap in an `httpx.MockTransport` and can prove that no
request leaves the process. The key only ever sits in a request header; it is
never logged, and no provider error text is passed on (OpenAI's 401 body
echoes part of the key), only Jolt's own fixed messages (`ModelCallError`).
"""

from __future__ import annotations

import abc
import base64
import json
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from jolt.config import Settings, get_settings

ANTHROPIC_VERSION = "2026-10-21"
# Server-side refusal fallback, the `"default"` form (routes by refusal category).
ANTHROPIC_FALLBACK_BETA = "server-side-fallback-2026-07-01"
# The models-list probe keeps the version it always sent.
ANTHROPIC_PROBE_VERSION = "2023-06-01"

# Typed error codes of the key-backed routes, with their HTTP status.
KEY_REQUIRED = "key_required"
KEY_INVALID = "key_invalid"
OUT_OF_CREDIT = "out_of_credit"
RATE_LIMITED = "rate_limited"
SPEND_LIMIT = "spend_limit"
PROVIDER_ERROR = "provider_error"

ERROR_STATUS = {
    KEY_REQUIRED: 403,
    KEY_INVALID: 422,
    OUT_OF_CREDIT: 402,
    RATE_LIMITED: 429,
    SPEND_LIMIT: 429,
    PROVIDER_ERROR: 502,
}


class ModelCallError(Exception):
    """A key-backed call that did not produce an answer. `detail` is Jolt's own
    text (never the provider's, never the key)."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        status: Optional[int] = None,
        retry_after: Optional[int] = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status or ERROR_STATUS.get(code, 502)
        self.retry_after = retry_after


# --------------------------------------------------------------------------- #
# Request / response
# --------------------------------------------------------------------------- #
@dataclass
class TextPart:
    text: str


@dataclass
class FilePart:
    """An original page: an image (JPEG, PNG, WebP) or a PDF."""

    media_type: str
    data: bytes
    filename: str = "page"

    @property
    def is_pdf(self) -> bool:
        return self.media_type == "application/pdf"

    def b64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")


@dataclass
class ModelRequest:
    system: str
    parts: list = field(default_factory=list)  # FilePart | TextPart, in order
    max_tokens: int = 4000
    effort: str = "low"  # low | medium
    timeout: float = 45.0


@dataclass
class ModelResponse:
    text: str
    model_id: Optional[str]
    input_tokens: int = 0
    output_tokens: int = 0
    truncated: bool = False


@dataclass
class ValidationResult:
    ok: bool
    last4: str
    detail: str = ""


# --------------------------------------------------------------------------- #
# Adapters
# --------------------------------------------------------------------------- #
class ProviderAdapter(abc.ABC):
    name: str
    label: str

    @abc.abstractmethod
    async def validate(
        self,
        key: str,
        *,
        settings: Optional[Settings] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> ValidationResult: ...

    @abc.abstractmethod
    def model_for(self, settings: Settings) -> str: ...

    @abc.abstractmethod
    async def complete(
        self,
        key: str,
        request: ModelRequest,
        *,
        settings: Settings,
        client: httpx.AsyncClient,
    ) -> ModelResponse: ...

    @staticmethod
    def _last4(key: str) -> str:
        return key[-4:] if len(key) >= 4 else key

    async def _probe(
        self, url: str, headers: dict, key: str, client: Optional[httpx.AsyncClient]
    ) -> ValidationResult:
        if client is None:
            async with httpx.AsyncClient(timeout=10.0) as own:
                resp = await own.get(url, headers=headers)
        else:
            resp = await client.get(url, headers=headers, timeout=10.0)
        ok = resp.status_code == 200
        return ValidationResult(
            ok=ok,
            last4=self._last4(key),
            detail="" if ok else f"provider returned {resp.status_code}",
        )

    async def _post(
        self, client: httpx.AsyncClient, url: str, headers: dict, body: dict, timeout: float
    ) -> dict:
        try:
            resp = await client.post(url, headers=headers, json=body, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise ModelCallError(
                PROVIDER_ERROR,
                f"{self.label} did not answer within {int(timeout)} s; try again",
                status=504,
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelCallError(
                PROVIDER_ERROR, f"could not reach {self.label}; try again later"
            ) from exc
        if resp.status_code != 200:
            raise self._error_for(resp)
        try:
            data = resp.json()
        except ValueError as exc:
            raise ModelCallError(PROVIDER_ERROR, f"{self.label} sent an unreadable answer") from exc
        if not isinstance(data, dict):
            raise ModelCallError(PROVIDER_ERROR, f"{self.label} sent an unreadable answer")
        return data

    def _error_for(self, resp: httpx.Response) -> ModelCallError:
        """Map a provider status to a typed error. The body is inspected (for
        the out-of-credit cases), never repeated."""
        status = resp.status_code
        error = _error_body(resp)
        if status in (401, 403):
            return ModelCallError(
                KEY_INVALID,
                f"{self.label} rejected the stored API key; add a valid key in Settings",
            )
        if status == 402 or self._is_out_of_credit(status, error):
            return ModelCallError(
                OUT_OF_CREDIT,
                f"the {self.label} account behind this key is out of credit; add credit "
                "with the provider or use another key",
            )
        if status == 429:
            return ModelCallError(
                RATE_LIMITED,
                f"{self.label} is rate limiting this key; try again shortly",
                retry_after=_retry_after(resp),
            )
        if status == 404:
            return ModelCallError(
                PROVIDER_ERROR, f"{self.label} does not offer the model Jolt asked for"
            )
        if status == 413:
            return ModelCallError(PROVIDER_ERROR, f"the request was too large for {self.label}")
        if status >= 500:
            return ModelCallError(
                PROVIDER_ERROR, f"{self.label} is unavailable right now; try again later"
            )
        return ModelCallError(PROVIDER_ERROR, f"{self.label} refused the request ({status})")

    def _is_out_of_credit(self, status: int, error: dict) -> bool:
        return False


class AnthropicAdapter(ProviderAdapter):
    name = "anthropic"
    label = "Anthropic"

    async def validate(
        self,
        key: str,
        *,
        settings: Optional[Settings] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> ValidationResult:
        base = (settings or get_settings()).anthropic_api_base.rstrip("/")
        headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_PROBE_VERSION}
        return await self._probe(f"{base}/v1/models", headers, key, client)

    def model_for(self, settings: Settings) -> str:
        return settings.anthropic_model

    async def complete(
        self,
        key: str,
        request: ModelRequest,
        *,
        settings: Settings,
        client: httpx.AsyncClient,
    ) -> ModelResponse:
        content: list[dict[str, Any]] = []
        for part in request.parts:
            if isinstance(part, FilePart):
                kind = "document" if part.is_pdf else "image"
                content.append(
                    {
                        "type": kind,
                        "source": {
                            "type": "base64",
                            "media_type": part.media_type,
                            "data": part.b64(),
                        },
                    }
                )
            else:
                content.append({"type": "text", "text": part.text})
        body: dict[str, Any] = {
            "model": self.model_for(settings),
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": [{"role": "user", "content": content}],
        }
        headers = {
            "x-api-key": key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        if settings.anthropic_send_effort:
            body["output_config"] = {"effort": request.effort}
        if settings.anthropic_refusal_fallback:
            body["fallbacks"] = "default"
            headers["anthropic-beta"] = ANTHROPIC_FALLBACK_BETA
        base = settings.anthropic_api_base.rstrip("/")
        data = await self._post(client, f"{base}/v1/messages", headers, body, request.timeout)

        if data.get("stop_reason") == "refusal":
            raise ModelCallError(PROVIDER_ERROR, "the model declined this request")
        blocks = data.get("content") or []
        text = "".join(
            b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
        )
        usage = data.get("usage") or {}
        input_tokens = sum(
            int(usage.get(k) or 0)
            for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        )
        return ModelResponse(
            text=text,
            model_id=data.get("model") or self.model_for(settings),
            input_tokens=input_tokens,
            output_tokens=int(usage.get("output_tokens") or 0),
            truncated=data.get("stop_reason") == "max_tokens",
        )

    def _is_out_of_credit(self, status: int, error: dict) -> bool:
        # Anthropic answers a drained prepaid balance with 400
        # invalid_request_error "Your credit balance is too low ...".
        message = str(error.get("message") or "").lower()
        return (status == 400 and "credit balance" in message) or error.get(
            "type"
        ) == "billing_error"


class OpenAIAdapter(ProviderAdapter):
    name = "openai"
    label = "OpenAI"

    async def validate(
        self,
        key: str,
        *,
        settings: Optional[Settings] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> ValidationResult:
        base = (settings or get_settings()).openai_api_base.rstrip("/")
        headers = {"Authorization": f"Bearer {key}"}
        return await self._probe(f"{base}/v1/models", headers, key, client)

    def model_for(self, settings: Settings) -> str:
        return settings.openai_model

    async def complete(
        self,
        key: str,
        request: ModelRequest,
        *,
        settings: Settings,
        client: httpx.AsyncClient,
    ) -> ModelResponse:
        content: list[dict[str, Any]] = []
        for part in request.parts:
            if isinstance(part, FilePart):
                url = f"data:{part.media_type};base64,{part.b64()}"
                if part.is_pdf:
                    content.append(
                        {"type": "input_file", "filename": part.filename, "file_data": url}
                    )
                else:
                    content.append({"type": "input_image", "image_url": url})
            else:
                content.append({"type": "input_text", "text": part.text})
        body: dict[str, Any] = {
            "model": self.model_for(settings),
            "instructions": request.system,
            "input": [{"role": "user", "content": content}],
            "max_output_tokens": request.max_tokens,
            "text": {"format": {"type": "json_object"}},
            # Do not keep the user's notes on the provider side.
            "store": False,
        }
        if settings.openai_send_reasoning:
            body["reasoning"] = {"effort": request.effort}
        headers = {"Authorization": f"Bearer {key}", "content-type": "application/json"}
        base = settings.openai_api_base.rstrip("/")
        data = await self._post(client, f"{base}/v1/responses", headers, body, request.timeout)

        status = data.get("status")
        reason = (data.get("incomplete_details") or {}).get("reason")
        if status == "failed" or (status == "incomplete" and reason == "content_filter"):
            raise ModelCallError(PROVIDER_ERROR, "the model could not complete this request")
        texts: list[str] = []
        refused = False
        for item in data.get("output") or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for c in item.get("content") or []:
                if c.get("type") == "output_text":
                    texts.append(c.get("text", ""))
                elif c.get("type") == "refusal":
                    refused = True
        if refused and not texts:
            raise ModelCallError(PROVIDER_ERROR, "the model declined this request")
        usage = data.get("usage") or {}
        return ModelResponse(
            text="".join(texts),
            model_id=data.get("model") or self.model_for(settings),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            truncated=status == "incomplete" and reason == "max_output_tokens",
        )

    def _is_out_of_credit(self, status: int, error: dict) -> bool:
        # OpenAI answers an exhausted quota or billing hard limit with 429
        # `insufficient_quota` (a plain rate limit is 429 too).
        return error.get("code") in ("insufficient_quota", "billing_hard_limit_reached")


def _error_body(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    error = data.get("error") if isinstance(data, dict) else None
    return error if isinstance(error, dict) else {}


def _retry_after(resp: httpx.Response) -> int:
    raw = resp.headers.get("retry-after", "")
    try:
        return max(1, min(3600, int(float(raw))))
    except ValueError:
        return 30


def parse_json_answer(text: str) -> Optional[dict]:
    """The JSON object in a model's answer: the whole text, or the outermost
    `{...}` when the model wrapped it in prose or a code fence."""
    text = text.strip()
    for candidate in (text, text[text.find("{") : text.rfind("}") + 1]):
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


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
