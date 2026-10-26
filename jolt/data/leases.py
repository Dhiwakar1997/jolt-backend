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
        source.claim_count += 1
        source.claimed_at = _now()
        return await self._repo.replace(source, etag=source.etag)

    async def release_processed(self, source: Source, active_extraction_id: str) -> Source | None:
        """Point the source at its new read and clear the lease.

        A pending read-again request is answered by this read if it was made
        before the sync claimed the source. One made while the sync was already
        running is kept, and the source goes back to `unprocessed` so the next
        sync reads it again (BE-05).
        """
        answered = source.reread_requested_at is None or (
            source.claimed_at is not None and source.reread_requested_at <= source.claimed_at
        )
        if answered:
            source.processing_status = ProcessingStatus.PROCESSED
            source.reread_requested_at = None
            source.reread_preferred_model = None
        else:
            source.processing_status = ProcessingStatus.UNPROCESSED
        source.lease_expires_at = None
        source.claim_count = 0
        source.failed_reason = None
        source.active_extraction_id = active_extraction_id
        return await self._repo.replace(source, etag=source.etag)


class ReviewLeases:
    def __init__(self, repo: ReviewsRepository) -> None:
        self._repo = repo

    async def claim(self, review: Review) -> Review | None:
        # Pending gradings have no lease field yet; a plain status check suffices
        # for the MVP (gradings are applied idempotently, so a double claim is safe).
        if review.status != ReviewStatus.PENDING_GRADING:
            return None
        return review
