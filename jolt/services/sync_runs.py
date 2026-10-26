"""Sync runs: record each nightly sync and report on them (BE-04).

A run belongs to one MCP token (so its `client_label` is the token's label):

* `jolt_sync` opens a run, or continues the token's open one, and closes it as
  `finished` when the plan it returns has nothing left. Calling it again soon
  after, with still nothing to do, does not open another (empty) run.
* Every other sync tool called with that token adds to the open run (sources
  processed, questions written, gradings submitted, concepts added or changed,
  errors). A call shortly after the run finished still counts towards it, so a
  model that checks `jolt_sync` early and then keeps working is not lost.
* A run with no tool call for `SYNC_RUN_IDLE_SECONDS` is closed as
  `incomplete`, lazily: by the next `jolt_sync` or the next status read. No
  timer, no job (the single-service design).

`users.first_sync_at` is set when a run first closes after doing something, or
closes `finished` (the client connected and found nothing to do).

Recording is best effort: a failure to update the run never fails the tool
call that triggered it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from jolt import telemetry
from jolt.auth.context import UserContext
from jolt.domain.models import SyncChange, SyncRun, SyncRunStatus
from jolt.runtime import Runtime

logger = logging.getLogger("jolt.sync")

MAX_ERRORS = 50
MAX_ERROR_CHARS = 500
MAX_CHANGES = 500
_RETRIES = 4
DEFAULT_CLIENT_LABEL = "MCP client"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def add_error(run: SyncRun, message: str) -> None:
    if len(run.errors) < MAX_ERRORS:
        run.errors.append(message.strip()[:MAX_ERROR_CHARS])


def add_change(run: SyncRun, change: SyncChange) -> None:
    if len(run.changes) < MAX_CHANGES:
        run.changes.append(change)


def concept_title(concept) -> str:
    """How a change names its concept: the agent's title, else its text (80 chars)."""
    if concept.title:
        return concept.title
    text = " ".join(concept.text.split())
    return text if len(text) <= 80 else text[:79].rstrip() + "…"


class SyncRunService:
    def __init__(self, rt: Runtime) -> None:
        self._rt = rt
        self._repo = rt.repos.sync_runs
        self._idle = timedelta(seconds=rt.settings.sync_run_idle_seconds)

    # -- lifecycle ------------------------------------------------------------
    async def start(self, user: UserContext) -> SyncRun:
        """Open a new run for this token (`jolt_sync`)."""
        run = SyncRun(
            user_id=user.user_id,
            token_id=user.token_id,
            client_label=user.token_label or DEFAULT_CLIENT_LABEL,
        )
        created = await self._repo.create(run)
        telemetry.record_sync_run(user.user_id, "started", created.client_label)
        return created

    async def current(self, user: UserContext) -> Optional[SyncRun]:
        """The run a tool call made with this token counts towards, if any."""
        latest = await self._repo.latest_for_token(user.user_id, user.token_id)
        if latest is None:
            return None
        now = _now()
        if latest.status == SyncRunStatus.RUNNING:
            return latest if now - latest.last_activity_at <= self._idle else None
        if latest.status == SyncRunStatus.FINISHED and latest.finished_at is not None:
            return latest if now - latest.finished_at <= self._idle else None
        return None

    async def record(self, user: UserContext, mutate: Callable[[SyncRun], None]) -> None:
        """Apply `mutate` to the token's current run (no-op without one)."""
        try:
            run = await self.current(user)
            if run is not None:
                await self.update(run, mutate)
        except Exception:  # never fail the tool call over bookkeeping
            logger.warning("could not record sync activity", exc_info=True)

    async def update(self, run: SyncRun, mutate: Callable[[SyncRun], None]) -> SyncRun:
        """Etag-guarded read-modify-write; re-reads and retries on a lost race."""
        for _ in range(_RETRIES):
            mutate(run)
            run.last_activity_at = max(run.last_activity_at, _now())
            saved = await self._repo.replace(run, etag=run.etag)
            if saved is not None:
                return saved
            fresh = await self._repo.get(run.id, run.user_id)
            if fresh is None:
                return run
            run = fresh
        logger.warning("sync run %s: gave up after %d lost races", run.id, _RETRIES)
        return run

    async def finish(self, run: SyncRun) -> SyncRun:
        def close(r: SyncRun) -> None:
            r.status = SyncRunStatus.FINISHED
            r.finished_at = _now()

        run = await self.update(run, close)
        telemetry.record_sync_run(run.user_id, "finished", run.client_label)
        await self._mark_first_sync(run.user_id, run.finished_at or _now())
        return run

    async def close_stale(self, user_id: str) -> None:
        """Close the user's runs that went idle (lazy, see module docstring)."""
        now = _now()
        for run in await self._repo.list_running(user_id):
            if now - run.last_activity_at <= self._idle:
                continue
            minutes = int(self._idle.total_seconds() // 60)

            def close(r: SyncRun) -> None:
                if r.status != SyncRunStatus.RUNNING:
                    return
                r.status = SyncRunStatus.INCOMPLETE
                r.finished_at = r.last_activity_at
                add_error(
                    r,
                    f"The client stopped without a final jolt_sync; closed after "
                    f"{minutes} idle minutes.",
                )

            # Not `update`: closing must keep last_activity_at as it was.
            for _ in range(_RETRIES):
                close(run)
                saved = await self._repo.replace(run, etag=run.etag)
                if saved is not None:
                    run = saved
                    telemetry.record_sync_run(user_id, "incomplete", run.client_label)
                    break
                fresh = await self._repo.get(run.id, run.user_id)
                if fresh is None or fresh.status != SyncRunStatus.RUNNING:
                    run = None
                    break
                run = fresh
            if run is not None and run.did_work:
                await self._mark_first_sync(user_id, run.finished_at or now)

    async def _mark_first_sync(self, user_id: str, at: datetime) -> None:
        user = await self._rt.repos.users.get_user(user_id)
        if user is None or user.first_sync_at is not None:
            return
        user.first_sync_at = at
        try:
            await self._rt.repos.users.replace(user, etag=user.etag)  # lost race: fine
        except Exception:
            logger.warning("could not set first_sync_at for %s", user_id, exc_info=True)

    # -- reads ----------------------------------------------------------------
    async def recent(self, user_id: str, limit: int) -> list[SyncRun]:
        await self.close_stale(user_id)
        return await self._repo.list_recent(user_id, limit)
