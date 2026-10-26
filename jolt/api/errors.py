"""Error body and documented error responses for the REST contract.

Every non-2xx response from `/v1` is FastAPI's `{"detail": ...}` body: a string
for errors raised by handlers and auth, a list of field errors for request
validation (422, `HTTPValidationError`). These constants only document that in
the OpenAPI contract; they do not change behaviour.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ErrorResponse(BaseModel):
    detail: str


def _err(description: str) -> dict[str, Any]:
    return {"model": ErrorResponse, "description": description}


UNAUTHORIZED = {401: _err("Missing or invalid bearer token")}
NOT_FOUND = {404: _err("Not found, or not owned by the caller")}
BAD_REQUEST = {400: _err("Request rejected by a business rule")}
CONFLICT = {409: _err("Conflicts with an earlier request (e.g. a reused request id)")}


# --------------------------------------------------------------------------- #
# Key-backed routes (BE-11): typed errors with a machine-readable `code`.
# --------------------------------------------------------------------------- #
class KeyBackedErrorResponse(BaseModel):
    detail: str
    code: str


def _typed(description: str) -> dict[str, Any]:
    return {"model": KeyBackedErrorResponse, "description": description}


def typed_or(other: str, description: str) -> dict[str, Any]:
    """A typed error, or another documented body on the same status."""
    refs = [
        {"$ref": "#/components/schemas/KeyBackedErrorResponse"},
        {"$ref": f"#/components/schemas/{other}"},
    ]
    return {
        "description": description,
        "content": {"application/json": {"schema": {"anyOf": refs}}},
    }


KEY_BACKED_ERRORS = {
    402: _typed("`out_of_credit`: the provider account behind the key has no credit"),
    403: _typed("`key_required`: no API key stored (no outbound call was made)"),
    422: typed_or(
        "HTTPValidationError",
        "`key_invalid`: the provider rejected the stored key; or request validation",
    ),
    429: typed_or(
        "ErrorResponse",
        "`rate_limited` (the provider) or `spend_limit` (the daily cap), with Retry-After; "
        "or Jolt's own rate limit",
    ),
    502: _typed("`provider_error`: the provider failed or its answer could not be used"),
    504: _typed("`provider_error`: the provider did not answer in time"),
}
