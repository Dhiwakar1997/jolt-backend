"""BE-10 security review fixes (docs/SECURITY.md). One section per finding."""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from fastapi.testclient import TestClient

from jolt.api.limits import _client_ip
from jolt.api.security import redact_secrets, redact_span
from jolt.config import DEFAULT_PEPPER, Settings
from jolt.credentials.vault import VaultClient
from jolt.data.blob import BlobGateway, page_blob_path
from jolt.domain.models import TokenKind
from jolt.main import create_app
from jolt.mcp import context as mcp_context
from jolt.mcp import server as mcp_server
from jolt.mcp.input_limits import InputTooLarge, check_tool_inputs
from jolt.runtime import Runtime
from jolt.services.accounts import AccountService
from jolt.services.maintenance import MaintenanceService
from tests.conftest import dev_sign_in

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra"


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, storage_mode="memory", dev_auth_enabled=True, **overrides)


def _client(settings: Settings | None = None) -> TestClient:
    settings = settings or _settings()
    return TestClient(create_app(settings=settings, runtime=Runtime.in_memory(settings)))


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Token pepper: never the placeholder with real storage
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("pepper", [DEFAULT_PEPPER, "short-but-not-default"])
def test_api_refuses_a_placeholder_or_short_pepper_with_azure_storage(pepper):
    settings = Settings(_env_file=None, storage_mode="azure", token_hash_pepper=pepper)
    with pytest.raises(ValueError, match="TOKEN_HASH_PEPPER"):
        settings.check_serving_secrets()
    app = create_app(settings=settings, runtime=Runtime.in_memory(settings))
    with pytest.raises(ValueError, match="TOKEN_HASH_PEPPER"):
        with TestClient(app):
            pass  # the lifespan refuses to start


def test_pepper_check_passes_for_a_real_pepper_and_memory_mode():
    Settings(
        _env_file=None, storage_mode="azure", token_hash_pepper="x" * 64
    ).check_serving_secrets()
    Settings(_env_file=None, storage_mode="memory").check_serving_secrets()


# --------------------------------------------------------------------------- #
# API docs and live schema: off in prod
# --------------------------------------------------------------------------- #
def test_docs_and_openapi_are_off_in_prod_and_on_in_dev():
    with _client(Settings(_env_file=None, storage_mode="memory", jolt_env="prod")) as c:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert c.get(path).status_code == 404, path
        assert c.get("/health").status_code == 200
    with _client() as c:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert c.get(path).status_code == 200, path
    forced = Settings(_env_file=None, storage_mode="memory", jolt_env="prod", api_docs_enabled=True)
    assert forced.docs_enabled


# --------------------------------------------------------------------------- #
# Security headers
# --------------------------------------------------------------------------- #
def test_api_responses_carry_security_headers():
    with _client() as c:
        token = dev_sign_in(c)["token"]
        for resp in (c.get("/v1/me", headers=bearer(token)), c.get("/v1/me")):
            h = resp.headers
            assert h["x-content-type-options"] == "nosniff"
            assert h["x-frame-options"] == "DENY"
            assert h["referrer-policy"] == "no-referrer"
            assert h["cache-control"] == "no-store"
            assert h["content-security-policy"].startswith("default-src 'none'")
            assert "strict-transport-security" not in h  # dev
        docs = c.get("/docs")
        assert "content-security-policy" not in docs.headers  # Swagger UI needs its scripts
        assert docs.headers["x-content-type-options"] == "nosniff"
        assert "cache-control" not in c.get("/health").headers


def test_hsts_in_prod_and_headers_on_a_429():
    settings = Settings(
        _env_file=None, storage_mode="memory", jolt_env="prod", rate_limit_ip_per_minute=1
    )
    with _client(settings) as c:
        assert c.get("/v1/me").headers["strict-transport-security"].startswith("max-age=")
        limited = c.get("/v1/me")
        assert limited.status_code == 429
        assert limited.headers["x-content-type-options"] == "nosniff"


# --------------------------------------------------------------------------- #
# 422 responses do not echo the rejected input
# --------------------------------------------------------------------------- #
def test_validation_errors_do_not_echo_secrets():
    secret = "sk-ant-api03-" + "s" * 40
    with _client() as c:
        token = dev_sign_in(c)["token"]
        resp = c.post("/v1/settings/api-key", json={"key": secret}, headers=bearer(token))
        assert resp.status_code == 422
        assert secret not in resp.text
        assert resp.json()["detail"][0]["loc"] == ["body", "provider"]
        assert "input" not in resp.json()["detail"][0]

        too_long = c.post("/v1/auth/google", json={"id_token": "e" * 9000})
        assert too_long.status_code == 422 and "eeeeeeee" not in too_long.text
        key_too_long = c.post(
            "/v1/settings/api-key",
            json={"provider": "anthropic", "key": "k" * 600},
            headers=bearer(token),
        )
        assert key_too_long.status_code == 422 and "kkkkkkkk" not in key_too_long.text


# --------------------------------------------------------------------------- #
# ?token= and session_id never reach logs or telemetry
# --------------------------------------------------------------------------- #
def test_redaction_covers_token_and_session_id():
    line = 'GET /mcp/sse?token=jolt_live_abc&x=1 / POST /mcp/messages/?session_id=deadbeef "'
    out = redact_secrets(line)
    assert "jolt_live_abc" not in out and "deadbeef" not in out
    assert "token=[redacted]&x=1" in out and "session_id=[redacted]" in out


def test_access_log_redacts_session_ids():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "POST", "/mcp/messages/?session_id=0123abcd", "1.1", 202),
        None,
    )
    mcp_server.RedactTokenFilter().filter(record)
    assert "0123abcd" not in record.getMessage()


def test_request_spans_sent_to_app_insights_are_redacted():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from jolt.telemetry import instrument_app

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    settings = _settings()
    app = create_app(settings=settings, runtime=Runtime.in_memory(settings))
    instrument_app(app, tracer_provider=provider)
    secret = "jolt_live_" + "q" * 40
    with TestClient(app) as c:
        assert c.get(f"/mcp/sse?token={secret}&x=1").status_code == 401
        c.post("/mcp/messages/?session_id=feedfacecafe", json={})
        c.get("/v1/sync/status?tz=UTC")
    spans = exporter.get_finished_spans()
    dumped = json.dumps([dict(s.attributes or {}) for s in spans])
    assert spans and secret not in dumped and "feedfacecafe" not in dumped
    assert "token=[redacted]" in dumped and "session_id=[redacted]" in dumped
    assert "tz=UTC" in dumped  # other parameters are kept


def test_redact_span_ignores_spans_that_do_not_record():
    class Off:
        def is_recording(self):
            return False

        def set_attribute(self, *_):  # pragma: no cover - must not be called
            raise AssertionError

    redact_span(Off(), {"query_string": b"token=x"})
    redact_span(None, {"query_string": b"token=x"})


# --------------------------------------------------------------------------- #
# App tokens expire after APP_TOKEN_IDLE_DAYS without use (GAPS #7)
# --------------------------------------------------------------------------- #
async def _age(runtime: Runtime, user_id: str, kind: TokenKind, days: float) -> None:
    then = datetime.now(timezone.utc) - timedelta(days=days)
    for record in await runtime.repos.tokens.list_for_user(user_id, kind):
        record.created_at = then
        record.last_used_at = then if record.last_used_at else None
        await runtime.repos.tokens.replace(record)


async def test_idle_app_tokens_expire_and_are_deleted():
    settings = _settings(app_token_idle_days=30)
    runtime = Runtime.in_memory(settings)
    with TestClient(create_app(settings=settings, runtime=runtime)) as c:
        me = dev_sign_in(c)
        assert c.get("/v1/me", headers=bearer(me["token"])).status_code == 200
        await _age(runtime, me["user_id"], TokenKind.APP, 29)
        assert c.get("/v1/me", headers=bearer(me["token"])).status_code == 200  # renews
        (record,) = await runtime.repos.tokens.list_for_user(me["user_id"], TokenKind.APP)
        assert datetime.now(timezone.utc) - record.last_used_at < timedelta(minutes=1)

        await _age(runtime, me["user_id"], TokenKind.APP, 31)
        resp = c.get("/v1/me", headers=bearer(me["token"]))
        assert resp.status_code == 401 and resp.json()["detail"] == "token expired; sign in again"
        assert await runtime.repos.tokens.list_for_user(me["user_id"], TokenKind.APP) == []
        # Signing in again issues a fresh token.
        again = dev_sign_in(c, me["user"]["email"])
        assert c.get("/v1/me", headers=bearer(again["token"])).status_code == 200


async def test_never_used_app_token_expires_from_issue_and_zero_disables():
    settings = _settings(app_token_idle_days=0)
    runtime = Runtime.in_memory(settings)
    with TestClient(create_app(settings=settings, runtime=runtime)) as c:
        me = dev_sign_in(c)
        await _age(runtime, me["user_id"], TokenKind.APP, 400)
        assert c.get("/v1/me", headers=bearer(me["token"])).status_code == 200

    settings = _settings(app_token_idle_days=7)
    runtime = Runtime.in_memory(settings)
    with TestClient(create_app(settings=settings, runtime=runtime)) as c:
        me = dev_sign_in(c)
        await _age(runtime, me["user_id"], TokenKind.APP, 8)  # issued, never used
        assert c.get("/v1/me", headers=bearer(me["token"])).status_code == 401


async def test_mcp_tokens_do_not_expire():
    settings = _settings(app_token_idle_days=1)
    runtime = Runtime.in_memory(settings)
    service = AccountService(runtime)
    user = await service.sign_in_dev(f"agent-{uuid.uuid4().hex[:8]}@example.com")
    issued = await service.create_mcp_token(user.user.id, "Agent")
    await _age(runtime, user.user.id, TokenKind.MCP, 365)
    mcp_context.set_runtime(runtime)
    ctx = await mcp_context.authenticate_mcp(issued.token)
    assert ctx.user_id == user.user.id


async def test_maintenance_deletes_expired_app_tokens_only():
    settings = _settings(app_token_idle_days=30)
    runtime = Runtime.in_memory(settings)
    service = AccountService(runtime)
    old = await service.sign_in_dev("old@example.com")
    fresh = await service.sign_in_dev("fresh@example.com")
    await service.create_mcp_token(old.user.id, "Nightly")
    await _age(runtime, old.user.id, TokenKind.APP, 45)
    await _age(runtime, old.user.id, TokenKind.MCP, 45)

    dry = await MaintenanceService(runtime).sweep(dry_run=True)
    assert dry.expired_app_tokens == 1
    assert len(await runtime.repos.tokens.list_for_user(old.user.id, TokenKind.APP)) == 1

    report = await MaintenanceService(runtime).sweep()
    assert report.expired_app_tokens == 1
    assert await runtime.repos.tokens.list_for_user(old.user.id, TokenKind.APP) == []
    assert len(await runtime.repos.tokens.list_for_user(old.user.id, TokenKind.MCP)) == 1
    assert len(await runtime.repos.tokens.list_for_user(fresh.user.id, TokenKind.APP)) == 1


# --------------------------------------------------------------------------- #
# Per-IP rate limit: the client cannot choose its X-Forwarded-For bucket
# --------------------------------------------------------------------------- #
def test_client_ip_uses_the_trusted_hop_of_x_forwarded_for():
    scope = {
        "client": ("6.6.6.6", 1),  # what uvicorn --proxy-headers makes of the leftmost entry
        "headers": [(b"x-forwarded-for", b"6.6.6.6, 203.0.113.9")],
    }
    assert _client_ip(scope, 1) == "203.0.113.9"
    assert _client_ip(scope, 0) == "6.6.6.6"
    assert _client_ip({"client": ("10.0.0.1", 1), "headers": []}, 1) == "10.0.0.1"


def test_spoofed_x_forwarded_for_does_not_reset_the_ip_limit():
    with _client(_settings(rate_limit_ip_per_minute=2, trusted_proxy_hops=1)) as c:
        codes = [
            c.get("/v1/me", headers={"X-Forwarded-For": f"10.0.0.{i}, 198.51.100.7"}).status_code
            for i in range(3)
        ]
        assert codes == [401, 401, 429]
        other = c.get("/v1/me", headers={"X-Forwarded-For": "198.51.100.8"})
        assert other.status_code == 401  # a different real client has its own bucket


# --------------------------------------------------------------------------- #
# MCP tool input sizes
# --------------------------------------------------------------------------- #
def test_check_tool_inputs_limits():
    check_tool_inputs({"concepts": [{"text": "x", "track_id": "t"}] * 200, "source_ids": ["s"]})
    check_tool_inputs({"markdown": "m" * 500_000, "limit": 200})
    for bad in (
        {"concepts": [{"text": "x"}] * 201},
        {"markdown": "m" * 500_001},
        {"concepts": [{"text": "t" * 20_001}]},
        {"questions": [{"stem": "s", "options": ["o"] * 11}]},
        {"syllabus": [{"concept_key": "k" * 201, "label": "l"}]},
        {"limit": 0},
        {"limit": 10_000},
        {"errors": ["e"] * 51},
    ):
        with pytest.raises(InputTooLarge, match="input too large|limit must be"):
            check_tool_inputs(bad)


async def test_oversized_tool_calls_fail_before_writing(runtime, mcp_user):
    create_app(settings=runtime.settings, runtime=runtime)  # installs this app's limits
    mcp = mcp_server.build_mcp()
    tracks_before = len(runtime.cosmos.container("tracks").all_items())
    concepts_before = len(runtime.cosmos.container("concepts").all_items())
    with pytest.raises(Exception, match="input too large: concepts has 201 items"):
        await mcp.call_tool(
            "jolt_log_session",
            {"source_ids": [], "concepts": [{"text": "x", "track_id": "t"}] * 201},
        )
    with pytest.raises(Exception, match="input too large: markdown"):
        await mcp.call_tool("jolt_store_extraction", {"source_id": "s", "markdown": "m" * 500_001})
    with pytest.raises(Exception, match="limit must be between"):
        await mcp.call_tool("jolt_get_unprocessed_sources", {"limit": 5000})
    assert len(runtime.cosmos.container("tracks").all_items()) == tracks_before
    assert len(runtime.cosmos.container("concepts").all_items()) == concepts_before
    # A normal call still works.
    content = await mcp.call_tool("jolt_get_tracks", {})
    assert "tracks" in json.loads(content[0].text)


# --------------------------------------------------------------------------- #
# Google ID tokens: verified email, iat / nbf not in the future
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("claims", "reason"),
    [
        ({"email_verified": False}, "not verified"),
        ({"email_verified": None}, "not verified"),
        ({"iat": 4_102_444_800, "exp": 4_102_448_400}, "invalid token"),  # issued in 2100
        ({"nbf": 4_102_444_800}, "invalid token"),
    ],
    ids=["unverified-email", "no-email-verified-claim", "iat-in-future", "nbf-in-future"],
)
def test_google_sign_in_rejects_more_bad_claims(client, google_signer, claims, reason):
    resp = client.post("/v1/auth/google", json={"id_token": google_signer.mint(**claims)})
    assert resp.status_code == 401, resp.text
    assert reason in resp.json()["detail"]


def test_google_sign_in_accepts_legacy_string_email_verified(client, google_signer):
    token = google_signer.mint(email_verified="true", sub="legacy-sub")
    assert client.post("/v1/auth/google", json={"id_token": token}).status_code == 200


# --------------------------------------------------------------------------- #
# Key Vault: delete purges; a failed delete keeps the reference
# --------------------------------------------------------------------------- #
class _Poller:
    async def wait(self):
        return None


class _FakeSecrets:
    def __init__(self, delete_error=None, purge_error=None):
        self.calls: list[tuple[str, str]] = []
        self.delete_error = delete_error
        self.purge_error = purge_error

    async def begin_delete_secret(self, name):
        self.calls.append(("delete", name))
        if self.delete_error:
            raise self.delete_error
        return _Poller()

    async def purge_deleted_secret(self, name):
        self.calls.append(("purge", name))
        if self.purge_error:
            raise self.purge_error


def _vault(fake) -> VaultClient:
    vault = VaultClient(Settings(_env_file=None, keyvault_url="https://kv.example/"))
    vault._client = fake
    return vault


def _http_error(status: int) -> HttpResponseError:
    err = HttpResponseError(message=f"status {status}")
    err.status_code = status
    return err


async def test_vault_delete_purges_and_tolerates_missing_secrets(caplog):
    fake = _FakeSecrets()
    await _vault(fake).delete_key("u1", "anthropic")
    assert fake.calls == [("delete", "apikey-u1-anthropic"), ("purge", "apikey-u1-anthropic")]

    gone = _FakeSecrets(ResourceNotFoundError("nope"), ResourceNotFoundError("nope"))
    await _vault(gone).delete_key("u1", "anthropic")  # never stored: fine
    assert [c[0] for c in gone.calls] == ["delete", "purge"]

    protected = _FakeSecrets(purge_error=_http_error(403))
    with caplog.at_level(logging.WARNING, logger="jolt.vault"):
        await _vault(protected).delete_key("u1", "openai")
    assert "apikey-u1-openai deleted but not purged (HTTP 403)" in caplog.text


async def test_failed_vault_delete_keeps_the_cosmos_reference(runtime, mcp_user):
    from jolt.domain.models import ApiKeyRef
    from jolt.services.credentials import CredentialService

    await runtime.repos.api_keys.create(
        ApiKeyRef(user_id=mcp_user.user_id, provider="anthropic", key_ref="apikey-x", last4="abcd")
    )
    runtime.vault = _vault(_FakeSecrets(delete_error=_http_error(403)))
    with pytest.raises(HttpResponseError):
        await CredentialService(runtime).delete(mcp_user.user_id, "anthropic")
    assert await runtime.repos.api_keys.for_provider(mcp_user.user_id, "anthropic") is not None


# --------------------------------------------------------------------------- #
# Blob SAS: HTTPS only, one blob, least permission, short lifetime
# --------------------------------------------------------------------------- #
def _gateway(ttl: int = 900) -> BlobGateway:
    from azure.storage.blob.aio import BlobServiceClient

    key = "a2V5a2V5a2V5a2V5a2V5a2V5a2V5a2V5a2V5a2V5a2V5a2V5a2V5a2V5a2V5a2V5"
    conn = (
        "DefaultEndpointsProtocol=https;AccountName=stjolttest;"
        f"AccountKey={key};EndpointSuffix=core.windows.net"
    )
    return BlobGateway(
        service=BlobServiceClient.from_connection_string(conn),
        container="sources",
        settings=Settings(_env_file=None, blob_sas_ttl_seconds=ttl),
        account_key=key,
    )


def _sas(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


async def test_sas_urls_are_https_only_single_blob_and_short_lived():
    gw = _gateway(ttl=900)
    now = datetime.now(timezone.utc)
    put = await gw.upload_url("u1/s1/0-note.png")
    sas = _sas(put)
    assert urlparse(put).path == "/sources/u1/s1/0-note.png"
    assert sas["spr"] == "https" and sas["sr"] == "b"
    assert set(sas["sp"]) == {"c", "w"}  # no read, list or delete
    expiry = datetime.fromisoformat(sas["se"].replace("Z", "+00:00"))
    assert timedelta(minutes=14) < expiry - now <= timedelta(minutes=15, seconds=5)

    get = _sas(await gw.read_url("u1/s1/0-note.png", content_type="image/png"))
    assert get["sp"] == "r" and get["spr"] == "https" and get["rsct"] == "image/png"

    export = _sas(await gw.read_url("exports/u1/x.json", ttl_seconds=3600))
    expiry = datetime.fromisoformat(export["se"].replace("Z", "+00:00"))
    assert timedelta(minutes=59) < expiry - now <= timedelta(hours=1, seconds=5)
    await gw.close()


@pytest.mark.parametrize(
    "filename",
    ["../../other-user/x.png", "..\\..\\x.png", "/etc/passwd", "%2e%2e/x", "..", "a/../../b.pdf"],
)
def test_blob_paths_cannot_escape_the_users_source_folder(filename):
    path = page_blob_path("user-1", "source-1", filename, 2)
    assert path.startswith("user-1/source-1/2-")
    rest = path[len("user-1/source-1/") :]
    assert "/" not in rest and ".." not in rest


# --------------------------------------------------------------------------- #
# Infra and image
# --------------------------------------------------------------------------- #
def test_key_vault_access_is_logged():
    kv = (INFRA / "modules" / "keyvault.bicep").read_text()
    assert "Microsoft.Insights/diagnosticSettings" in kv
    assert re.search(r"categoryGroup: 'audit'", kv)
    main = (INFRA / "main.bicep").read_text()
    assert "logAnalyticsWorkspaceId: monitoring.outputs.id" in main


def test_dev_sign_in_cannot_reach_prod_through_bicep():
    main = (INFRA / "main.bicep").read_text()
    assert "var devAuth = devAuthEnabled && environmentName != 'prod'" in main
    assert "devAuthEnabled: devAuth\n" in main
    assert re.search(r"devAuthReaderPrincipalId: devAuth \?", main)
    with pytest.raises(ValueError, match="must not be set when JOLT_ENV=prod"):
        Settings(_env_file=None, jolt_env="prod", storage_mode="memory", dev_auth_enabled=True)


def test_container_app_trusts_one_proxy_hop():
    bicep = (INFRA / "modules" / "containerapp.bicep").read_text()
    assert "{ name: 'TRUSTED_PROXY_HOPS', value: '1' }" in bicep


def test_image_runs_unprivileged_and_carries_no_secrets():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert re.search(r"^USER 10001$", dockerfile, flags=re.M)
    assert "--no-server-header" in dockerfile
    env_lines = re.findall(r"^(?:ENV|ARG)\s+(.+)$", dockerfile, flags=re.M)
    assert not any(re.search(r"KEY|SECRET|PEPPER|PASSWORD", line) for line in env_lines)
    ignored = (ROOT / ".dockerignore").read_text().split()
    assert {".env", ".env.*", ".git/", "tests/"} <= set(ignored)


# --------------------------------------------------------------------------- #
# MCP sessions are bound to the credential that opened them (mcp >= 1.27.2)
# --------------------------------------------------------------------------- #
@pytest.fixture
def mcp_server_url():
    import socket
    import threading
    import time

    import uvicorn

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    settings = _settings()
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings=settings), port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    server.force_exit = True
    thread.join(timeout=10)


async def _mcp_token(http, email: str) -> str:
    app_token = (await http.post("/v1/auth/dev", json={"email": email})).json()["app_token"]
    resp = await http.post("/v1/mcp-tokens", json={"label": "Agent"}, headers=bearer(app_token))
    return resp.json()["token"]


async def _endpoint(lines) -> str:
    # Pull lines off one iterator without leaving an `async for`: closing the
    # iterator would close the event stream, and with it the session.
    while True:
        line = await lines.__anext__()
        if line.startswith("data: /mcp/messages/"):
            return line[len("data: ") :]


_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}


async def test_mcp_session_accepts_messages_only_from_its_own_credential(mcp_server_url):
    import asyncio

    import httpx

    async with httpx.AsyncClient(base_url=mcp_server_url, timeout=10) as http:
        alice = await _mcp_token(http, "alice@example.com")
        eve = await _mcp_token(http, "eve@example.com")

        async with http.stream("GET", "/mcp/sse", headers=bearer(alice)) as stream:
            lines = stream.aiter_lines()  # keep a reference: collecting it closes the stream
            endpoint = await asyncio.wait_for(_endpoint(lines), 5)
            assert (await http.post(endpoint, json=_INIT)).status_code == 404
            assert (await http.post(endpoint, json=_INIT, headers=bearer(eve))).status_code == 404
            assert (await http.post(endpoint, json=_INIT, headers=bearer(alice))).status_code == 202

        # A ?token= session cannot be bound (its client posts without a
        # credential); it relies on the secret, redacted session id.
        async with http.stream("GET", f"/mcp/sse?token={alice}") as stream:
            lines = stream.aiter_lines()  # keep a reference: collecting it closes the stream
            endpoint = await asyncio.wait_for(_endpoint(lines), 5)
            assert (await http.post(endpoint, json=_INIT, headers=bearer(eve))).status_code == 404
            assert (await http.post(endpoint, json=_INIT)).status_code == 202
