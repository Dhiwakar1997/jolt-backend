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
