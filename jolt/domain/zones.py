"""The learner's time zone (BE-08, GAPS #8).

"Today" (done today, the daily cap, streaks, the heatmap), the next days of a
session summary and the upload date of a session (decision D6) are all days in
the learner's `time_zone` setting. Unset or unknown zones fall back to UTC, the
behaviour before BE-08.
"""

from __future__ import annotations

from datetime import timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jolt.domain.models import UserSettings


def zone_named(name: str | None) -> tzinfo:
    """`ZoneInfo(name)`, or UTC when the name is empty or unknown."""
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def user_zone(settings: UserSettings | None) -> tzinfo:
    return zone_named(settings.time_zone if settings is not None else None)


def is_valid_zone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True
