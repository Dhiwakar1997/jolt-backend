"""A stand-in for the Anthropic and OpenAI APIs, for local runs and tests (BE-11).

    python scripts/fake_provider.py --port 8043
    ANTHROPIC_API_BASE=http://127.0.0.1:8043 OPENAI_API_BASE=http://127.0.0.1:8043 \\
        STORAGE_MODE=memory DEV_AUTH_ENABLED=true PUBLIC_BASE_URL=http://localhost:8000 \\
        uvicorn jolt.main:app --port 8000
    python scripts/smoke.py --base-url http://localhost:8000 --api-key sk-fake-local-0001

Serves what Jolt calls, with canned answers and no model behind them:

* `GET /v1/models` (key validation at `POST /v1/settings/api-key`), both providers;
* `POST /v1/messages` (Anthropic Messages API);
* `POST /v1/responses` (OpenAI Responses API).

A key is accepted when it starts with `sk-fake` (`--key-prefix`); any other key
gets 401, like a real provider. A key ending in `-nocredit` gets the
provider's out-of-credit answer, one ending in `-ratelimit` a 429. The answer
is feedback JSON when the system prompt is Jolt's feedback coach, else a read
of the note (markdown, one concept, one question). The request is checked for
the shape Jolt must send (model, files as image / document inputs).

`canned()` is the whole behaviour, so tests can serve it through an
`httpx.MockTransport` without opening a port. Standard library only.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

DEFAULT_KEY_PREFIX = "sk-fake"

FEEDBACK = {
    "feedback": "You first said it spreads load; now you explain which keys move when a node "
    "joins.",
    "next_step": "Explain in your own words why only the neighbouring keys move.",
}
READ = {
    "markdown": "# Consistent hashing\n\nKeys and nodes share one ring; a key belongs to the "
    "next node clockwise.",
    "confidence": 0.9,
    "concepts": [
        {
            "title": "Consistent hashing ring",
            "text": "Keys and nodes are hashed onto one ring, and a key belongs to the next node "
            "clockwise, so adding a node moves only nearby keys.",
            "concept_key": "consistent-hashing",
            "page": 1,
            "questions": [
                {
                    "stem": "Why does adding a node to a consistent-hashing ring move few keys?",
                    "options": [
                        "Only keys between the new node and its predecessor change owner",
                        "Every key is rehashed onto a new ring",
                        "Keys are sorted again by value",
                        "Nodes vote on which keys to keep",
                    ],
                    "correct_index": 0,
                    "expected_answer": "Only the keys between the new node and its predecessor "
                    "change owner; the rest stay where they are.",
                }
            ],
        }
    ],
}


def _key(headers: dict[str, str]) -> str:
    lowered = {k.lower(): v for k, v in headers.items()}
    if "x-api-key" in lowered:
        return lowered["x-api-key"]
    auth = lowered.get("authorization", "")
    return auth[7:] if auth.lower().startswith("bearer ") else auth


def _error(status: int, kind: str, message: str, code: Optional[str] = None) -> tuple[int, dict]:
    body: dict[str, Any] = {"type": kind, "message": message}
    if code:
        body["code"] = code
    return status, {"type": "error", "error": body}


def canned(
    method: str,
    path: str,
    headers: dict[str, str],
    body: Optional[dict],
    *,
    key_prefix: str = DEFAULT_KEY_PREFIX,
    feedback: Optional[dict] = None,
    read: Optional[dict] = None,
) -> tuple[int, dict]:
    """(status, JSON body) for one request, as the real provider would answer."""
    key = _key(headers)
    openai = path.startswith("/v1/responses") or (
        path == "/v1/models" and "x-api-key" not in {k.lower() for k in headers}
    )
    if not key.startswith(key_prefix):
        # OpenAI's real 401 echoes part of the key: Jolt must never pass it on.
        return _error(401, "authentication_error", f"Incorrect API key provided: {key[:8]}****")
    if key.endswith("-nocredit"):
        if openai:
            quota = "insufficient_quota"
            return _error(429, quota, "You exceeded your current quota", quota)
        return _error(400, "invalid_request_error", "Your credit balance is too low")
    if key.endswith("-ratelimit"):
        return _error(429, "rate_limit_error", "Too many requests")
    if method == "GET" and path == "/v1/models":
        return 200, {"data": [{"id": "fake-model"}]}
    if method != "POST" or path not in ("/v1/messages", "/v1/responses") or body is None:
        return _error(404, "not_found_error", "not found")
    if not body.get("model"):
        return _error(400, "invalid_request_error", "model is required")

    if path == "/v1/messages":
        system = str(body.get("system") or "")
        content = body["messages"][0]["content"]
        files = [c for c in content if c.get("type") in ("image", "document")]
    else:
        system = str(body.get("instructions") or "")
        content = body["input"][0]["content"]
        files = [c for c in content if c.get("type") in ("input_image", "input_file")]
    is_feedback = "feedback coach" in system
    if not is_feedback and not files:
        return _error(400, "invalid_request_error", "a read needs the note's pages")
    answer = json.dumps((feedback or FEEDBACK) if is_feedback else (read or READ))
    model = f"{body['model']}-fake"
    if path == "/v1/messages":
        return 200, {
            "id": "msg_fake",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": answer}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1200, "output_tokens": 300},
        }
    return 200, {
        "id": "resp_fake",
        "object": "response",
        "status": "completed",
        "model": model,
        "output": [
            {"type": "reasoning", "summary": []},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": answer}],
            },
        ],
        "usage": {"input_tokens": 1100, "output_tokens": 250},
    }


def serve(port: int, *, host: str = "127.0.0.1", key_prefix: str = DEFAULT_KEY_PREFIX):
    """A ThreadingHTTPServer answering with `canned()` (call serve_forever())."""

    class Handler(BaseHTTPRequestHandler):
        def _answer(self, method: str) -> None:
            length = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw) if raw else None
            except ValueError:
                body = None
            status, payload = canned(
                method, self.path, dict(self.headers.items()), body, key_prefix=key_prefix
            )
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            self._answer("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._answer("POST")

        def log_message(self, *_args) -> None:  # never print headers (keys)
            return

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=8043)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--key-prefix", default=DEFAULT_KEY_PREFIX)
    args = parser.parse_args()
    server = serve(args.port, host=args.host, key_prefix=args.key_prefix)
    print(f"fake provider on http://{args.host}:{args.port} (keys: {args.key_prefix}*)")
    server.serve_forever()


if __name__ == "__main__":
    main()
