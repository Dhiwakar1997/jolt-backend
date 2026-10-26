"""Maintenance sweep (BE-10): abandoned uploads and data retention."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from jolt import maintenance as maintenance_cli
from jolt.config import Settings
from jolt.domain.models import Extraction
from jolt.services.accounts import AccountService
from jolt.services.maintenance import MaintenanceService
from jolt.services.uploads import PageSpec, UploadService

NOW = datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc)


async def _user(runtime, retention_days=None) -> str:
    import uuid

    signed = await AccountService(runtime).sign_in_dev(f"u-{uuid.uuid4().hex[:8]}@example.com")
    user = await runtime.repos.users.get_user(signed.user.id)
    user.settings.data_retention_days = retention_days
    await runtime.repos.users.replace(user)
    return user.id


async def _source(runtime, user_id, name, *, created, confirmed=True, pages=1) -> str:
    specs = []
    payloads = []
    for i in range(pages):
        data = f"{name}-{i}".encode()
        payloads.append(data)
        specs.append(
            PageSpec(f"{name}-{i}.png", "image/png", hashlib.sha256(data).hexdigest(), len(data))
        )
    uploads = UploadService(runtime)
    ticket = await uploads.request_upload(user_id, specs)
    source = await runtime.repos.sources.get_source(ticket.source_id, user_id)
    for page, data in zip(source.page_list(), payloads):
        if confirmed or page.index == 0:  # an abandoned upload may have some pages
            runtime.blob.put(page.blob_path, data, "image/png")
    if confirmed:
        source = await uploads.confirm_upload(user_id, source.id)
        source.confirmed_at = created
    source.created_at = created
    await runtime.repos.sources.replace(source)
    await runtime.repos.extractions.create(Extraction(source_id=source.id, markdown="# read"))
    return source.id


async def test_sweep_deletes_abandoned_uploads_and_expired_sources(runtime):
    keep_forever = await _user(runtime)  # no retention
    week = await _user(runtime, retention_days=7)

    abandoned = await _source(
        runtime, keep_forever, "half", created=NOW - timedelta(hours=25), confirmed=False, pages=2
    )
    fresh_unconfirmed = await _source(
        runtime, keep_forever, "pending", created=NOW - timedelta(hours=2), confirmed=False
    )
    ancient_kept = await _source(runtime, keep_forever, "old", created=NOW - timedelta(days=400))
    expired = await _source(runtime, week, "expired", created=NOW - timedelta(days=8), pages=3)
    recent = await _source(runtime, week, "recent", created=NOW - timedelta(days=6))

    report = await MaintenanceService(runtime).sweep(now=NOW)

    assert report.abandoned_uploads == 1
    assert report.users_with_retention == 1
    assert report.expired_sources == 1
    assert report.extractions == 2  # one read each on the two deleted sources
    sources = runtime.repos.sources
    assert await sources.get_source(abandoned, keep_forever) is None
    assert await sources.get_source(expired, week) is None
    for uid, sid in (
        (keep_forever, fresh_unconfirmed),
        (keep_forever, ancient_kept),
        (week, recent),
    ):
        assert await sources.get_source(sid, uid) is not None
    assert await runtime.repos.extractions.history_for_source(expired) == []
    assert await runtime.repos.extractions.history_for_source(recent) != []
    paths = runtime.blob.paths()
    assert not any(f"/{abandoned}/" in p or f"/{expired}/" in p for p in paths)
    assert any(f"/{recent}/" in p for p in paths)
    assert report.blobs == 1 + 3  # the page that arrived + three pages

    # Idempotent: a second sweep finds nothing.
    again = await MaintenanceService(runtime).sweep(now=NOW)
    assert (again.abandoned_uploads, again.expired_sources, again.blobs) == (0, 0, 0)


async def test_dry_run_counts_and_deletes_nothing(runtime):
    uid = await _user(runtime, retention_days=1)
    sid = await _source(runtime, uid, "old", created=NOW - timedelta(days=3))
    gone = await _source(runtime, uid, "x", created=NOW - timedelta(days=2), confirmed=False)
    report = await MaintenanceService(runtime).sweep(now=NOW, dry_run=True)
    assert (report.dry_run, report.abandoned_uploads, report.expired_sources) == (True, 1, 1)
    assert report.blobs == 0
    assert await runtime.repos.sources.get_source(sid, uid) is not None
    assert await runtime.repos.sources.get_source(gone, uid) is not None


def test_cli_runs_one_sweep_and_prints_the_report(capsys):
    settings = Settings(_env_file=None, storage_mode="memory")
    assert maintenance_cli.main(["--dry-run"], settings=settings) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["dry_run"] is True and out["abandoned_uploads"] == 0


def test_cli_exits_non_zero_on_failure(monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("cosmos unreachable")

    monkeypatch.setattr(maintenance_cli, "run", boom)
    settings = Settings(_env_file=None, storage_mode="memory")
    assert maintenance_cli.main([], settings=settings) == 1
