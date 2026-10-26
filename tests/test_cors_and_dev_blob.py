"""App CORS from settings, and the memory-mode dev blob endpoints (BE-02)."""

from __future__ import annotations

import asyncio
import hashlib
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from jolt.config import Settings
from jolt.main import create_app
from jolt.runtime import Runtime

APP = "http://localhost:5173"
PREVIEW_RE = r"^https://deploy-preview-[0-9]+--jolt-app\.netlify\.app$"


def _client(**overrides) -> TestClient:
    settings = Settings(_env_file=None, storage_mode="memory", **overrides)
    return TestClient(create_app(settings=settings, runtime=Runtime.in_memory(settings)))


def _preflight(c: TestClient, origin: str):
    return c.options(
        "/v1/tracks",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )


def test_cors_allows_configured_origins_only():
    with _client(cors_origins=f"{APP}, https://jolt-app.netlify.app") as c:
        ok = _preflight(c, APP)
        assert ok.status_code == 200
        assert ok.headers["access-control-allow-origin"] == APP
        assert "authorization" in ok.headers["access-control-allow-headers"].lower()
        assert "access-control-allow-credentials" not in ok.headers

        netlify = _preflight(c, "https://jolt-app.netlify.app")
        assert netlify.headers["access-control-allow-origin"] == "https://jolt-app.netlify.app"

        denied = _preflight(c, "https://evil.example")
        assert denied.status_code == 400
        assert "access-control-allow-origin" not in denied.headers


def test_cors_origin_regex_covers_netlify_previews():
    with _client(cors_origins=APP, cors_origin_regex=PREVIEW_RE) as c:
        preview = "https://deploy-preview-42--jolt-app.netlify.app"
        assert _preflight(c, preview).headers["access-control-allow-origin"] == preview
        assert _preflight(c, "https://deploy-preview-42--other.netlify.app").status_code == 400


def test_default_cors_origin_is_the_vite_dev_server():
    assert Settings(_env_file=None).cors_origin_list == [APP]


@pytest.fixture
def served():
    """Memory mode with PUBLIC_BASE_URL: upload URLs are served by /_dev/blob."""
    with _client(public_base_url="http://testserver", dev_auth_enabled=True) as c:
        signed_in = c.post("/v1/auth/dev", json={"email": "blob@example.com"}).json()
        c.headers["Authorization"] = f"Bearer {signed_in['app_token']}"
        yield c


def _path(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.path}?{parts.query}"


def test_dev_blob_round_trip(served: TestClient):
    data = b"\x89PNG fake"
    sha = hashlib.sha256(data).hexdigest()
    ticket = served.post(
        "/v1/uploads/request",
        json={"filename": "page 1.png", "content_type": "image/png", "sha256": sha},
    ).json()
    url = ticket["upload_url"]
    assert url.startswith("http://testserver/_dev/blob/sources/")
    assert url.split("?")[0].endswith("/0-page_1.png")  # page index + safe filename

    # Not yet uploaded: confirm fails like it would against Azure.
    assert served.post(f"/v1/uploads/{ticket['source_id']}/confirm", json={}).status_code == 400

    put = served.put(
        _path(url),
        content=data,
        headers={"x-ms-blob-type": "BlockBlob", "Content-Type": "image/png"},
    )
    assert put.status_code == 201
    confirm = served.post(f"/v1/uploads/{ticket['source_id']}/confirm", json={"sha256": sha})
    assert confirm.json() == {"status": "ready"}

    # The upload URL cannot read; a tampered signature cannot write.
    assert served.get(_path(url)).status_code == 403
    assert served.put(_path(url).replace("sig=", "sig=0"), content=b"x").status_code == 403
    assert served.put(_path(url).replace("sp=cw", "sp=r"), content=b"x").status_code == 403


def test_dev_blob_read_url(served: TestClient):
    rt = served.app.state.runtime
    path = "u1/s1/a.png"
    rt.blob.put(path, b"bytes", "image/png")
    read_url = asyncio.run(rt.blob.read_url(path))
    got = served.get(_path(read_url))
    assert got.status_code == 200 and got.content == b"bytes"
    assert got.headers["content-type"] == "image/png"
    assert served.put(_path(read_url), content=b"x").status_code == 403


def test_dev_blob_routes_absent_outside_memory_mode():
    settings = Settings(_env_file=None, storage_mode="azure")
    app = create_app(settings=settings, runtime=Runtime.in_memory(settings))
    with TestClient(app) as c:
        assert c.put("/_dev/blob/sources/x?sp=cw&sig=y", content=b"x").status_code == 404
    paths = {getattr(r, "path", "") for r in app.routes}
    assert not any(p.startswith("/_dev") for p in paths)


def test_without_public_base_url_urls_stay_opaque(client, auth):
    ticket = client.post(
        "/v1/uploads/request",
        json={"filename": "a.png", "content_type": "image/png", "sha256": "a" * 64},
        headers=auth,
    ).json()
    assert ticket["upload_url"].startswith("memory://blob/")
