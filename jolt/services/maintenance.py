"""Maintenance sweep: abandoned uploads and data retention (BE-10, BE-08 follow-up).

Run daily by the `jolt-maintenance` Container Apps Job (`python -m
jolt.maintenance`, infra/modules/containerapp.bicep). Three passes:

1. **Abandoned uploads.** A source whose upload was requested but never
   confirmed (`confirmed_at` unset) more than `ABANDONED_UPLOAD_HOURS` (24)
   ago: its blobs (any pages that did arrive) and its document are deleted.
   Such sources never appear in the app (BE-05 lists confirmed ones only).
2. **Data retention.** For each user with `settings.data_retention_days` set,
   every source uploaded (`confirmed_at`, else requested) more than that many
   days ago is deleted with its blobs and its reads (extractions). What was
   learned from it stays: concepts, questions, review history and FSRS state
   are the user's study record, not the upload, and sessions keep their (now
   dangling) source ids. A retention of None keeps uploads forever.

3. **Expired app tokens** (BE-10 security review): app tokens unused for more
   than `APP_TOKEN_IDLE_DAYS` are already refused at sign-in time
   (jolt/auth/context.py); the sweep deletes their documents so a lost
   device's token does not linger in the database. MCP tokens never expire.

Deletes are idempotent, so a sweep interrupted half way is finished by the next
one. Blob soft delete (and versioning in prod) keeps deleted blobs recoverable
for the configured days (docs/DEPLOY.md, "Backups and restore"); after that
they are gone. `dry_run` counts without deleting.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from jolt import telemetry
from jolt.auth.context import is_idle
from jolt.domain.models import Source, TokenKind
from jolt.runtime import Runtime

logger = logging.getLogger("jolt.maintenance")


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SweepReport:
    dry_run: bool = False
    abandoned_uploads: int = 0
    users_with_retention: int = 0
    expired_sources: int = 0
    extractions: int = 0
    blobs: int = 0
    expired_app_tokens: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


class MaintenanceService:
    def __init__(self, rt: Runtime) -> None:
        self._rt = rt

    async def sweep(self, *, now: Optional[datetime] = None, dry_run: bool = False) -> SweepReport:
        now = now or _now()
        report = SweepReport(dry_run=dry_run)
        await self._sweep_abandoned(now, report)
        await self._sweep_retention(now, report)
        await self._sweep_idle_app_tokens(now, report)
        if not dry_run:
            telemetry.record_maintenance("abandoned_upload", report.abandoned_uploads)
            telemetry.record_maintenance("expired_source", report.expired_sources)
            telemetry.record_maintenance("blob", report.blobs)
            telemetry.record_maintenance("expired_app_token", report.expired_app_tokens)
        logger.info("maintenance sweep: %s", report.as_dict())
        return report

    async def _sweep_abandoned(self, now: datetime, report: SweepReport) -> None:
        cutoff = now - timedelta(hours=self._rt.settings.abandoned_upload_hours)
        for source in await self._rt.repos.sources.list_unconfirmed_before(cutoff):
            if source.confirmed_at is not None:  # confirmed meanwhile
                continue
            report.abandoned_uploads += 1
            if not report.dry_run:
                await self._delete_source(source, report)

    async def _sweep_retention(self, now: datetime, report: SweepReport) -> None:
        for user in await self._rt.repos.users.list_all():
            days = user.settings.data_retention_days
            if not days:
                continue
            report.users_with_retention += 1
            cutoff = now - timedelta(days=days)
            for source in await self._rt.repos.sources.list_for_user(user.id):
                if source.confirmed_at is None:
                    continue  # pass 1 handles unconfirmed uploads
                if source.uploaded_at >= cutoff:
                    continue
                report.expired_sources += 1
                if not report.dry_run:
                    await self._delete_source(source, report)

    async def _sweep_idle_app_tokens(self, now: datetime, report: SweepReport) -> None:
        days = self._rt.settings.app_token_idle_days
        if days <= 0:
            return
        idle = timedelta(days=days)
        tokens = self._rt.repos.tokens
        for record in await tokens.list_kind_across_users(TokenKind.APP):
            if not is_idle(record, idle, now):
                continue
            report.expired_app_tokens += 1
            if not report.dry_run:
                await tokens.revoke(record.user_id, record.id)

    async def _delete_source(self, source: Source, report: SweepReport) -> None:
        """Blobs first, then reads, then the source: a failure leaves the source
        document in place, so the next sweep finds it again."""
        repos = self._rt.repos
        # Every page lives under `<user>/<source>/` (jolt/data/blob.py).
        prefix = f"{source.user_id}/{source.id}/"
        report.blobs += await self._rt.blob.delete_prefix(prefix)
        for page in source.page_list():  # any path outside it (older documents)
            if page.blob_path and not page.blob_path.startswith(prefix):
                if await self._rt.blob.stat(page.blob_path) is not None:
                    await self._rt.blob.delete(page.blob_path)
                    report.blobs += 1
        report.extractions += await repos.extractions.delete_partition(source.id)
        await repos.sources.delete(source.id, partition_value=source.user_id)
