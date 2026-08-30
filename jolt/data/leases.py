"""Lease acquire / release (LLD §6 leases, §8 concurrency).

A source or grading row is claimable if its status is `unprocessed`/`pending`
**or** it is `in_flight` with an expired lease. Acquisition is an etag-conditional
replace, so two concurrent syncs cannot both claim the same row: the loser gets a
None from `replace()` and moves on. Releasing clears the lease on success.

Expired leases are handled **lazily, not by a sweeper**: the claim check below
already treats an `in_flight` row whose `lease_expires_at` has passed as
claimable, so an interrupted sync's rows become claimable automatically on the
next `get_unprocessed_sources` / `get_pending_gradings` call. No timed job.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jolt.config import get_settings
from jolt.data.repositories.reviews import ReviewsRepository
from jolt.data.repositories.sources import SourcesRepository
from jolt.domain.models import (
    ProcessingStatus,
    Review,
    ReviewStatus,
    Source,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lease_expiry() -> datetime:
    return _now() + timedelta(seconds=get_settings().lease_ttl_seconds)


def _lease_available(status_ok: bool, lease_expires_at: datetime | None) -> bool:
    if status_ok:
        return True
    # in_flight but the lease has lapsed -> reclaimable
    return lease_expires_at is not None and lease_expires_at <= _now()


class SourceLeases:
    def __init__(self, repo: SourcesRepository) -> None:
        self._repo = repo

    async def claim(self, source: Source) -> Source | None:
        claimable = _lease_available(
            source.processing_status == ProcessingStatus.UNPROCESSED,
            source.lease_expires_at,
        )
        if not claimable:
            return None
        source.processing_status = ProcessingStatus.IN_FLIGHT
        source.lease_expires_at = _lease_expiry()
        return await self._repo.replace(source, etag=source.etag)

    async def release_processed(self, source: Source, active_extraction_id: str) -> Source | None:
        source.processing_status = ProcessingStatus.PROCESSED
        source.lease_expires_at = None
        source.active_extraction_id = active_extraction_id
        return await self._repo.replace(source, etag=source.etag)


class ReviewLeases:
    def __init__(self, repo: ReviewsRepository) -> None:
        self._repo = repo

    async def claim(self, review: Review) -> Review | None:
        claimable = _lease_available(
            review.status == ReviewStatus.PENDING_GRADING,
            review.free_text_locked_at and _lease_expiry(),
        )
        # Pending gradings use free_text_locked_at as their claim marker; a simple
        # status check suffices for MVP (no long-held grading lease field yet).
        if review.status != ReviewStatus.PENDING_GRADING:
            return None
        return review
