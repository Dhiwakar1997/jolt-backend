"""Nightly sync rules (BE-04): the plan `jolt_sync` hands the model, the upload
day used for session boundaries (decision D6), and sync health.

Pure: no I/O, no FastAPI, no Azure. The service layer gathers the rows and calls
`build_plan`; the MCP tool turns the result into JSON.

Re-reads (BE-05) are `PlanReread` items: sources the user asked to read again
(POST /v1/sources/{id}/reread). Asking puts the source back to `unprocessed`, so a
re-read is ordinary source work that the claim step hands out with
`supersede: true`; the plan lists those sources (with the preferred model) in
`reread_sources` and adds the supersede guidance whenever the list is non-empty.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------- #
# Per-step limits the plan advertises. Guidance for the model, sized so a batch
# of claimed sources is finished well inside the lease.
# --------------------------------------------------------------------------- #
SOURCES_PER_CLAIM = 5
GRADINGS_PER_CLAIM = 20
GRADINGS_PER_SUBMIT = 20
CONCEPTS_PER_SESSION = 20
QUESTIONS_PER_CONCEPT_MIN = 1
QUESTIONS_PER_CONCEPT_MAX = 3
OPTIONS_PER_QUESTION = 4

AGENDA_GUIDANCE = {
    "none": (
        "No agenda yet. Give every concept a short, stable concept_key: new keys are "
        "added to the agenda, which then becomes a draft."
    ),
    "draft": (
        "Draft agenda. Read it with jolt_get_agenda, tidy it into a clean syllabus "
        "(keep the existing concept_keys) and write it back with jolt_set_agenda "
        "mark_refined=true, then file concepts under its keys."
    ),
    "refined": (
        "Refined agenda. File concepts under its concept_keys; a concept with a new "
        "key is added to the agenda as a leaf."
    ),
    "locked": (
        "Locked agenda. File concepts under its existing concept_keys only: new keys "
        "are not added and jolt_set_agenda is refused."
    ),
}


def upload_day(moment: datetime) -> str:
    """The upload date (YYYY-MM-DD, UTC) a source belongs to, for decision D6."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).date().isoformat()


# --------------------------------------------------------------------------- #
# Plan inputs
# --------------------------------------------------------------------------- #
@dataclass
class PlanSource:
    source_id: str
    filename: str
    track_id: Optional[str]
    uploaded_at: datetime
    lease_expires_at: Optional[datetime] = None


@dataclass
class PlanReread:
    """A source the user asked to read again (BE-05). It is also one of the
    plan's claimable or claimed sources."""

    source_id: str
    title: str
    filename: str
    track_id: Optional[str]
    preferred_model: Optional[str]
    requested_at: Optional[datetime]


@dataclass
class PlanGrading:
    review_id: str
    track_id: Optional[str]


@dataclass
class PlanTrackInfo:
    track_id: str
    name: str
    agenda_status: str


# --------------------------------------------------------------------------- #
# Plan output
# --------------------------------------------------------------------------- #
@dataclass
class TrackLine:
    track_id: str
    name: str
    agenda_status: str
    unprocessed_sources: int
    pending_gradings: int
    rereads: int
    guidance: str


@dataclass
class PlanStep:
    step: int
    tool: str
    purpose: str
    limit: Optional[int]
    repeat: str


@dataclass
class PlanLimits:
    sources_per_claim: int = SOURCES_PER_CLAIM
    gradings_per_claim: int = GRADINGS_PER_CLAIM
    gradings_per_submit: int = GRADINGS_PER_SUBMIT
    concepts_per_session: int = CONCEPTS_PER_SESSION
    questions_per_concept_min: int = QUESTIONS_PER_CONCEPT_MIN
    questions_per_concept_max: int = QUESTIONS_PER_CONCEPT_MAX
    options_per_question: int = OPTIONS_PER_QUESTION
    lease_seconds: int = 900


@dataclass
class SyncPlan:
    done: bool
    unprocessed_sources: int
    pending_gradings: int
    rereads: int
    claimed_sources: list[PlanSource]
    in_flight_elsewhere: int
    unassigned_sources: int
    upload_dates: list[str]
    tracks: list[TrackLine]
    reread_sources: list[PlanReread]
    steps: list[PlanStep]
    limits: PlanLimits
    next: str = ""
    run_id: Optional[str] = None


@dataclass
class _Steps:
    items: list[PlanStep] = field(default_factory=list)

    def add(self, tool: str, purpose: str, *, limit: Optional[int], repeat: str) -> None:
        self.items.append(PlanStep(len(self.items) + 1, tool, purpose, limit, repeat))


def build_plan(
    *,
    claimable: list[PlanSource],
    claimed_by_run: list[PlanSource],
    in_flight_elsewhere: int,
    gradings: list[PlanGrading],
    rereads: list[PlanReread],
    tracks: dict[str, PlanTrackInfo],
    user_track_ids: list[str],
    lease_seconds: int,
) -> SyncPlan:
    """The ordered work list for one `jolt_sync` call.

    `claimable` are sources nobody holds; `claimed_by_run` are sources this run
    claimed and has not stored yet (their lease is live). Sources another client
    holds only show up as `in_flight_elsewhere`: they do not keep the run open.
    `rereads` are the read-again requests among `claimable` and `claimed_by_run`
    (a subset: they are counted as source work too).
    `tracks` maps every track id that may appear to its name and agenda status;
    `user_track_ids` are the caller's own tracks, all listed when some source has
    no track (so the model can pick one).
    """
    limits = PlanLimits(lease_seconds=lease_seconds)
    source_work = claimable + claimed_by_run
    done = not (source_work or gradings or rereads)

    # Counts by track, and which tracks the model needs to know about.
    involved: list[str] = []

    def involve(track_id: Optional[str]) -> None:
        if track_id and track_id in tracks and track_id not in involved:
            involved.append(track_id)

    for item in [*source_work, *rereads, *gradings]:
        involve(item.track_id)
    unassigned = sum(1 for s in source_work if not s.track_id)
    if unassigned or any(not r.track_id for r in rereads):
        for track_id in user_track_ids:
            involve(track_id)

    lines = [
        TrackLine(
            track_id=t,
            name=tracks[t].name,
            agenda_status=tracks[t].agenda_status,
            unprocessed_sources=sum(1 for s in source_work if s.track_id == t),
            pending_gradings=sum(1 for g in gradings if g.track_id == t),
            rereads=sum(1 for r in rereads if r.track_id == t),
            guidance=AGENDA_GUIDANCE.get(tracks[t].agenda_status, ""),
        )
        for t in involved
    ]

    steps = _Steps()
    if claimable:
        steps.add(
            "jolt_get_unprocessed_sources",
            "Claim a batch of sources (each is leased to you for lease_seconds). Call again "
            "after finishing a batch, until it returns no sources. A source marked "
            "supersede=true is a read again (see reread_sources).",
            limit=min(SOURCES_PER_CLAIM, len(claimable)),
            repeat="per batch",
        )
    if source_work:
        steps.add(
            "jolt_get_source_content",
            "Get signed read URLs for a claimed source (pages[], in order) and read every "
            "page yourself.",
            limit=None,
            repeat="per claimed source",
        )
    if rereads:
        steps.add(
            "jolt_diff_extractions",
            "For a read-again source (supersede=true), preview how your new reading's "
            "concepts line up with the track's existing ones before storing. Use its "
            "preferred_model if it names you; otherwise read it anyway.",
            limit=None,
            repeat="per reread source",
        )
    if source_work:
        steps.add(
            "jolt_store_extraction",
            "Store what you read as markdown, with an honest 0..1 confidence and your "
            "model_id; pass supersede=true when the claim said so. This marks the source "
            "processed and makes this read the active one.",
            limit=None,
            repeat="per claimed source",
        )
        steps.add(
            "jolt_log_session",
            "Log the source's concepts (one idea each, with track_id, title, concept_key "
            "and source_id). Sources from different upload dates get one session per "
            "date (the server splits them).",
            limit=CONCEPTS_PER_SESSION,
            repeat="per source or upload date",
        )
        steps.add(
            "jolt_create_questions",
            f"Write {QUESTIONS_PER_CONCEPT_MIN} to {QUESTIONS_PER_CONCEPT_MAX} questions per "
            f"new concept: a free-text stem, a complete expected_answer, and exactly "
            f"{OPTIONS_PER_QUESTION} options with one correct (correct_index).",
            limit=QUESTIONS_PER_CONCEPT_MAX,
            repeat="per concept",
        )
    if gradings:
        steps.add(
            "jolt_get_pending_gradings",
            "Fetch answers waiting for a grade. Call again after submitting, until it "
            "returns none.",
            limit=min(GRADINGS_PER_CLAIM, len(gradings)),
            repeat="per batch",
        )
        steps.add(
            "jolt_submit_gradings",
            "Grade each free-text answer against its expected_answer with the rubric "
            "(1 Again, 2 Hard, 3 Good, 4 Easy; when torn, the lower) and submit.",
            limit=GRADINGS_PER_SUBMIT,
            repeat="per batch",
        )
    if not done:
        steps.add(
            "jolt_sync",
            "Check what is left. Pass errors=[...] for anything you could not do. Follow "
            "the new plan until it says done.",
            limit=None,
            repeat="once, after the steps above",
        )

    upload_dates = sorted({upload_day(s.uploaded_at) for s in source_work})
    if done:
        next_hint = "Nothing left. The sync run is closed; stop here."
    else:
        parts = []
        if source_work:
            again = f" ({len(rereads)} to read again)" if rereads else ""
            parts.append(f"{len(source_work)} source(s) to read{again}")
        if gradings:
            parts.append(f"{len(gradings)} answer(s) to grade")
        next_hint = "Work left: " + ", ".join(parts) + ". Follow the steps in order."

    return SyncPlan(
        done=done,
        unprocessed_sources=len(claimable),
        pending_gradings=len(gradings),
        rereads=len(rereads),
        claimed_sources=claimed_by_run,
        in_flight_elsewhere=in_flight_elsewhere,
        unassigned_sources=unassigned,
        upload_dates=upload_dates,
        tracks=lines,
        reread_sources=rereads,
        steps=steps.items,
        limits=limits,
        next=next_hint,
    )


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
def sync_health(last_started_at: Optional[datetime], now: datetime, late_after: timedelta) -> str:
    """`never` with no run, `late` when the last run started more than
    `late_after` ago, else `ok`. Timezone-free on purpose: the schedule is daily,
    so 30 hours covers a slow or skipped-by-a-bit night anywhere."""
    if last_started_at is None:
        return "never"
    return "late" if now - last_started_at > late_after else "ok"


def parse_schedule_time(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def next_expected(now: datetime, schedule: time, tz: ZoneInfo) -> datetime:
    """The next time the scheduled task should fire: `schedule` local time in
    `tz`, today if still ahead, else tomorrow. Returned in UTC."""
    local_now = now.astimezone(tz)
    candidate = datetime.combine(local_now.date(), schedule, tzinfo=tz)
    if candidate <= local_now:
        candidate = datetime.combine(local_now.date() + timedelta(days=1), schedule, tzinfo=tz)
    return candidate.astimezone(timezone.utc)
