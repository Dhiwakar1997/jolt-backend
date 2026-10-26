"""scripts/smoke.py passes against a real local server in memory mode (BE-02).

Starts Uvicorn on a free port in a background thread (real HTTP, real MCP over
SSE, real PUT to the dev blob endpoint) and runs the same `run()` the deploy
workflow runs against Azure.
"""

from __future__ import annotations

import importlib.util
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from jolt.config import Settings
from jolt.main import create_app
from tests import dsa_fixture

ROOT = Path(__file__).resolve().parent.parent


def _load_smoke():
    spec = importlib.util.spec_from_file_location("jolt_smoke", ROOT / "scripts" / "smoke.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(tmp_path):
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    settings = Settings(
        _env_file=None,
        storage_mode="memory",
        public_base_url=base_url,
        app_version="smoke-test",
        dev_auth_enabled=True,
        # BE-09: the curated DSA track, from a synthetic Jolt checkout.
        dsa_seed_from=str(dsa_fixture.write_repo(tmp_path / "Jolt")),
    )
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings=settings), port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.05)
    yield base_url
    server.should_exit = True
    server.force_exit = True  # do not wait on lingering SSE streams
    thread.join(timeout=10)


async def test_smoke_round_trip(live_server):
    smoke = _load_smoke()
    out = await smoke.run(
        live_server, wait_seconds=10, expect_version="smoke-test", require_dsa=True
    )
    assert out["steps_passed"] == 54
    assert out["user_deleted"] is True and out["export_bytes"] > 0
    assert out["dsa_track_id"] == "dsa" and out["dsa_match"]
    assert out["version"] == "smoke-test"
    assert out["mcp_server_url"] == f"{live_server}/mcp/sse"


def test_smoke_cli_reports_failure_for_an_unreachable_server(capsys):
    smoke = _load_smoke()
    code = smoke.main(["--base-url", f"http://127.0.0.1:{_free_port()}", "--wait", "0"])
    assert code == 1
    assert "SMOKE FAILED" in capsys.readouterr().out


@pytest.fixture
def prod_like_server():
    """A server configured like prod: no dev sign-in, Google configured."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    settings = Settings(
        _env_file=None,
        storage_mode="memory",
        app_version="prod-smoke",
        google_oauth_client_id="prod-client.apps.googleusercontent.com",
        cors_origins="https://jolt.example.com",
    )
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings=settings), port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.05)
    yield base_url
    server.should_exit = True
    server.force_exit = True
    thread.join(timeout=10)


async def test_public_smoke_passes_against_a_prod_like_server(prod_like_server):
    smoke = _load_smoke()
    out = await smoke.run_public(
        prod_like_server,
        wait_seconds=10,
        expect_version="prod-smoke",
        origin="https://jolt.example.com",
        expect_dev_auth_off=True,
    )
    assert out["steps_passed"] == 8 and out["mode"] == "public"


async def test_public_smoke_fails_when_dev_auth_is_on(live_server):
    smoke = _load_smoke()
    with pytest.raises(smoke.SmokeFailure, match="(dev sign-in is reachable|not configured)"):
        await smoke.run_public(live_server, wait_seconds=10, expect_dev_auth_off=True)
