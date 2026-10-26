"""Export the API contract: REST (OpenAPI) and MCP tools (BE-01).

Writes, relative to the repo root:

* `contract/openapi.json` — FastAPI's OpenAPI document, limited to the `/v1`
  paths (the app's REST surface; `/health` and the `/mcp` mount are left out).
* `contract/mcp-tools.json` — every MCP tool, in registration order, with its
  name, description, input schema (as FastMCP advertises it to agents) and
  output schema (from `jolt/mcp/schemas.py`).

Usage:
    python scripts/export_contract.py           # (re)write the files
    python scripts/export_contract.py --check   # exit 1 if they are stale (CI)

The output is deterministic for a given set of pinned dependencies, so CI can
compare it byte for byte with the committed files. No Azure or `.env` needed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTRACT_DIR = ROOT / "contract"
OPENAPI_PATH = CONTRACT_DIR / "openapi.json"
MCP_TOOLS_PATH = CONTRACT_DIR / "mcp-tools.json"

sys.path.insert(0, str(ROOT))


def _settings():
    from jolt.config import Settings

    # Ignore any local .env so the export never depends on a developer's config.
    # Dev sign-in is on so `POST /v1/auth/dev` is in the contract (the front
    # end's dev mode uses it); servers mount it only when DEV_AUTH_ENABLED.
    return Settings(_env_file=None, storage_mode="memory", dev_auth_enabled=True)


def build_openapi() -> dict:
    from jolt.main import create_app

    spec = create_app(settings=_settings()).openapi()
    spec["paths"] = {p: ops for p, ops in spec["paths"].items() if p.startswith("/v1/")}
    return spec


def build_mcp_tools() -> dict:
    from jolt.mcp.schemas import TOOL_OUTPUTS, output_schema
    from jolt.mcp.server import _TOOLS, build_mcp

    advertised = {t.name: t for t in asyncio.run(build_mcp().list_tools())}
    missing = [name for name, _ in _TOOLS if name not in TOOL_OUTPUTS]
    if missing:
        raise SystemExit(f"no output schema in jolt/mcp/schemas.py for: {', '.join(missing)}")

    tools = []
    for name, _ in _TOOLS:
        tool = advertised[name]
        tools.append(
            {
                "name": name,
                "description": tool.description,
                "input_schema": tool.inputSchema,
                "output_schema": output_schema(name),
            }
        )
    return {
        "server": "jolt",
        "transport": "sse",
        "endpoint": "/mcp/sse",
        "auth": (
            "Authorization: Bearer <MCP token> (jolt_live_..., from POST /v1/mcp-tokens), or "
            "?token=<MCP token> on /mcp/sse for URL-only connectors (MCP_QUERY_TOKEN_ENABLED). "
            "App tokens are refused: opening /mcp/sse answers 401, tool calls fail. See "
            "docs/CONNECT.md."
        ),
        "errors": (
            "Tools whose output_schema is a union with {error} return that object for "
            "recoverable problems. Any other failure (missing/invalid token, unknown "
            "source, invalid grade) is raised and arrives as an MCP tool error "
            "(isError: true) with the message as text."
        ),
        "tools": tools,
    }


def render(doc: dict) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="fail if the committed contract is stale"
    )
    args = parser.parse_args(argv)

    os.environ.setdefault("STORAGE_MODE", "memory")
    outputs = {OPENAPI_PATH: render(build_openapi()), MCP_TOOLS_PATH: render(build_mcp_tools())}

    if args.check:
        stale = [
            p.relative_to(ROOT).as_posix()
            for p, text in outputs.items()
            if not p.exists() or p.read_text(encoding="utf-8") != text
        ]
        if stale:
            print(
                "Contract is stale: " + ", ".join(stale) + "\n"
                "Run `python scripts/export_contract.py` and commit the result.",
                file=sys.stderr,
            )
            return 1
        print("Contract is up to date.")
        return 0

    CONTRACT_DIR.mkdir(exist_ok=True)
    for path, text in outputs.items():
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
