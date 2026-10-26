"""BE-08: GET /v1/progress against a hand-computed fixture.

The fixture writes documents straight into the in-memory stores with fixed
timestamps, folds each studied card with the real FSRS fold, and asks for
progress at a fixed "now". Every expected number below is worked out by hand
from the definitions in `jolt/services/progress.py`; predicted recall uses the
FSRS forgetting curve R(t) = (1 + 19/81 * t / S) ** -0.5 with the FSRS initial
stabilities S(Again) = 0.40255, S(Good) = 3.173, S(Easy) = 15.69105 days (one
answer per card, so S is the initial stability of its grade).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from jolt.domain.models import (
    Concept,
    ConceptState,
    ConceptStatus,
    Review,
    ReviewStatus,
    Source,
    Track,
    TrackOrigin,
)
from jolt.domain.progress import retrievability, streak, week_start
from jolt.services.accounts import AccountService
from jolt.services.progress import ProgressService
from jolt.services.reviews import ReviewService

UTC = timezone.utc
NOW = datetime(2026, 3, 18, 12, 0, tzinfo=UTC)  # a Wednesday


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 3, day, hour, minute, tzinfo=UTC)


def R(days: float, stability: float) -> float:
    """The forgetting curve, written out independently of the service."""
    return (1 + (19 / 81) * days / stability) ** -0.5


# Hand-computed recall at NOW (2026-03-18 12:00 UTC):
A_NOW = R(8 + 2 / 24, 3.173)  # Good on 03-10 10:00: 8 d 2 h ago  -> 0.791170
B_NOW = R(2 + 3 / 24, 15.69105)  # Easy on 03-16 09:00: 2 d 3 h ago -> 0.984485
D_NOW = R(1 + 4 / 24, 0.40255)  # Again on 03-17 08:00: 1 d 4 h ago -> 0.771558
# Week of 03-09 ends 03-16 00:00: only A answered by then (E is retired).
A_WEEK1 = R(5 + 14 / 24, 3.173)  # 5 d 14 h after 03-10 10:00 -> 0.841330


def test_hand_values_are_what_they_say():
    assert A_NOW == pytest.approx(0.791170, abs=1e-6)
    assert B_NOW == pytest.approx(0.984485, abs=1e-6)
    assert D_NOW == pytest.approx(0.771558, abs=1e-6)
    assert A_WEEK1 == pytest.approx(0.841330, abs=1e-6)


async def _answer(rt, user_id, concept_id, when, grade, *, text="my own words", correct=True):
    return await rt.repos.reviews.create(
        Review(
            user_id=user_id,
            question_id=f"q-{concept_id}",
            concept_id=concept_id,
            created_at=when,
            free_text=text,
            free_text_locked_at=when,
            selected_index=0 if correct else 1,
            is_correct=correct,
            provisional_fsrs_grade=grade,
            stage2_at=when + timedelta(seconds=30),
            status=ReviewStatus.PENDING_GRADING,
        )
    )


async def build_fixture(rt) -> dict:
    accounts = AccountService(rt)
    user = (await accounts.sign_in_dev("progress@example.com")).user
    uid = user.id
    t1 = await rt.repos.tracks.create(
        Track(user_id=uid, name="Systems", created_at=at(1, 9), id="t1" + "0" * 30)
    )
    t2 = await rt.repos.tracks.create(
        Track(user_id=uid, name="Biology", created_at=at(2, 9), id="t2" + "0" * 30)
    )
    ids = {}
    for key, track in (("A", t1), ("B", t1), ("C", t1), ("D", t2), ("E", t2), ("X", t2)):
        concept = await rt.repos.concepts.create(
            Concept(track_id=track.id, text=f"Concept {key}", title=f"Concept {key}")
        )
        ids[key] = concept.id
        await rt.repos.concept_states.create(
            ConceptState(
                user_id=uid,
                concept_id=concept.id,
                track_id=track.id,
                due_at=NOW - timedelta(hours=1),
            )
        )
    # X was superseded by a later read: its card no longer counts anywhere.
    x = await rt.repos.concepts.get_concept(ids["X"], t2.id)
    x.status = ConceptStatus.SUPERSEDED
    await rt.repos.concepts.replace(x)

    await _answer(rt, uid, ids["A"], at(10, 10), 3)  # Good
    await _answer(rt, uid, ids["B"], at(16, 9), 4, text="")  # Easy, no words typed
    await _answer(rt, uid, ids["D"], at(17, 8), 1, correct=False)  # Again
    await _answer(rt, uid, ids["E"], at(4, 10), 4)  # Easy, before the 2-week window
    await _answer(rt, uid, ids["X"], at(17, 9), 3)  # a real answer, counted as one
    # Stage 1 only, today: words typed, not answered yet.
    await rt.repos.reviews.create(
        Review(
            user_id=uid,
            question_id="q-A",
            concept_id=ids["A"],
            created_at=at(18, 11),
            free_text="thinking out loud",
            free_text_locked_at=at(18, 11),
        )
    )
    reviews = ReviewService(rt)
    for key in ("A", "B", "D", "E", "X"):
        await reviews.recompute_card(uid, ids[key])
    # E is mastered (as if a long history had got it there) and so retired.
    e = await rt.repos.concept_states.for_concept(uid, ids["E"])
    e.mastered_at = at(4, 10)
    e.due_at = at(20, 10)  # pinned: FSRS fuzzes review intervals
    await rt.repos.concept_states.replace(e)

    # Sources: one with an active read, one still waiting, one never confirmed.
    for i, (read, confirmed) in enumerate([(True, True), (False, True), (True, False)]):
        await rt.repos.sources.create(
            Source(
                user_id=uid,
                filename=f"n{i}.jpg",
                content_type="image/jpeg",
                sha256="0" * 64,
                blob_path=f"{uid}/s{i}/0-n{i}.jpg",
                active_extraction_id="e" if read else None,
                confirmed_at=at(9, 9) if confirmed else None,
            )
        )
    # Someone else's answers never count.
    other = (await accounts.sign_in_dev("someone-else@example.com")).user
    await _answer(rt, other.id, ids["A"], at(18, 9), 3)
    return {"uid": uid, "t1": t1.id, "t2": t2.id, **ids}


async def test_progress_matches_the_hand_computed_fixture(runtime):
    f = await build_fixture(runtime)
    p = await ProgressService(runtime).progress(f["uid"], weeks=2, now=NOW)

    assert p.weeks == 2
    # Retention: active = studied and not retired = A, B, D (E retired, C new, X superseded).
    assert p.retention == pytest.approx((A_NOW + B_NOW + D_NOW) / 3, abs=1e-6)  # 0.849071
    assert [(w.week_start, round(w.retention, 6)) for w in p.retention_weekly] == [
        (date(2026, 3, 9), round(A_WEEK1, 6)),
        (date(2026, 3, 16), round((A_NOW + B_NOW + D_NOW) / 3, 6)),
    ]
    # 14 days, 03-05 .. 03-18; answers: A 03-10, B 03-16, D and X 03-17 (an answer
    # stays an answer after its concept is superseded); E's 03-04 is outside.
    assert [d.date for d in p.daily] == [date(2026, 3, 5) + timedelta(days=i) for i in range(14)]
    assert {d.date.day: d.reviews for d in p.daily if d.reviews} == {10: 1, 16: 1, 17: 2}
    assert p.reviews_total == 4
    assert p.reviews_this_week == 3  # since Monday 03-16
    # Words typed in the window: A, D, X and the stage-1-only answer today (B: none).
    assert p.answers_in_own_words == 4
    # Nothing answered yet today: 03-17 and 03-16 count, 03-15 breaks it.
    assert p.streak_days == 2
    assert (p.concepts_learning, p.concepts_mastered) == (3, 1)  # A, B, D / E
    assert p.sources_read == 1

    rows = {t.track_id: t for t in p.tracks}
    assert [t.track_id for t in p.tracks] == [f["t2"], f["t1"]]  # newest first
    systems, biology = rows[f["t1"]], rows[f["t2"]]
    assert (systems.name, systems.kind) == ("Systems", "notes")
    assert systems.retention == pytest.approx((A_NOW + B_NOW) / 2, abs=1e-6)  # 0.887828
    # Due now: A (Good on a new card: a 10-minute learning step), C (new, due);
    # B (Easy) is due in about 16 days.
    assert (systems.due, systems.mastered, systems.learning) == (2, 0, 2)
    assert biology.retention == pytest.approx(D_NOW, abs=1e-6)
    # D (Again: a 1-minute step) is due; E is retired, so not due; X is gone.
    assert (biology.due, biology.mastered, biology.learning) == (1, 1, 1)


async def test_retire_mastered_off_brings_mastered_concepts_into_retention(runtime):
    f = await build_fixture(runtime)
    user = await runtime.repos.users.get_user(f["uid"])
    user.settings.retire_mastered = False
    await runtime.repos.users.replace(user)
    p = await ProgressService(runtime).progress(f["uid"], weeks=2, now=NOW)
    e_now = R(14 + 2 / 24, 15.69105)  # Easy on 03-04 10:00: 14 d 2 h ago
    assert p.retention == pytest.approx((A_NOW + B_NOW + D_NOW + e_now) / 4, abs=1e-6)
    # Week of 03-09: E counts too now (answered 03-04, not retired).
    e_week1 = R(11 + 14 / 24, 15.69105)
    assert p.retention_weekly[0].retention == pytest.approx((A_WEEK1 + e_week1) / 2, abs=1e-6)
    # Mastered stays mastered (and out of "learning") whatever the setting.
    assert (p.concepts_learning, p.concepts_mastered) == (3, 1)
    # E is back in the queue but not due before 03-20: still only D is due.
    assert {t.track_id: t.due for t in p.tracks}[f["t2"]] == 1


async def test_days_follow_the_users_time_zone(runtime):
    f = await build_fixture(runtime)
    user = await runtime.repos.users.get_user(f["uid"])
    user.settings.time_zone = "Asia/Tokyo"  # UTC+9
    await runtime.repos.users.replace(user)
    # In Tokyo: B 03-16 18:00, D and X 03-17 17:00 / 18:00, NOW 03-18 21:00. An answer at
    # 03-17 16:00 UTC is 03-18 01:00 there: today in Tokyo, yesterday in UTC.
    await _answer(runtime, f["uid"], f["B"], at(17, 16), 3)
    p = await ProgressService(runtime).progress(f["uid"], weeks=1, now=NOW)
    days = {d.date.day: d.reviews for d in p.daily if d.reviews}
    assert days == {16: 1, 17: 2, 18: 1}
    assert p.streak_days == 3  # today (Tokyo) has an answer
    assert p.daily[-1].date == date(2026, 3, 18)


def test_streak_and_week_helpers():
    today = date(2026, 3, 18)
    days = {date(2026, 3, d) for d in (14, 15, 16, 17)}
    assert streak(days, today) == 4  # from yesterday while today is still open
    assert streak(days | {today}, today) == 5
    assert streak({date(2026, 3, 16)}, today) == 0
    assert streak(set(), today) == 0
    assert week_start(today) == date(2026, 3, 16)
    assert week_start(date(2026, 3, 16)) == date(2026, 3, 16)
    assert retrievability(3.0, None, NOW) == 0.0
    assert retrievability(3.0, NOW, NOW) == 1.0
    # R(S) = 0.9 by construction of the curve.
    assert retrievability(3.0, NOW - timedelta(days=3), NOW) == pytest.approx(0.9)


async def test_progress_route_shape_and_validation(client, auth):
    body = client.get("/v1/progress", headers=auth).json()
    assert body["weeks"] == 26 and len(body["daily"]) == 26 * 7
    assert body["retention"] == 0 and body["retention_weekly"] == []
    assert body["tracks"] == [] and body["streak_days"] == 0
    assert set(body) == {
        "weeks",
        "retention",
        "retention_weekly",
        "reviews_total",
        "reviews_this_week",
        "answers_in_own_words",
        "streak_days",
        "concepts_learning",
        "concepts_mastered",
        "sources_read",
        "daily",
        "tracks",
    }
    client.post("/v1/tracks", json={"name": "Empty"}, headers=auth)
    body = client.get("/v1/progress?weeks=1", headers=auth).json()
    assert len(body["daily"]) == 7
    assert body["tracks"][0] | {"track_id": None} == {
        "track_id": None,
        "name": "Empty",
        "kind": "notes",
        "retention": 0,
        "due": 0,
        "mastered": 0,
        "learning": 0,
    }
    for weeks in (0, 105, "x"):
        assert client.get(f"/v1/progress?weeks={weeks}", headers=auth).status_code == 422
    assert client.get("/v1/progress").status_code == 401


async def test_curated_tracks_the_user_studies_are_listed(runtime):
    uid = (await AccountService(runtime).sign_in_dev("curated@example.com")).user.id
    curated = await runtime.repos.tracks.create(Track(name="DSA", origin=TrackOrigin.JOLT))
    concept = await runtime.repos.concepts.create(Concept(track_id=curated.id, text="Two pointers"))
    await runtime.repos.concept_states.create(
        ConceptState(user_id=uid, concept_id=concept.id, track_id=curated.id, due_at=NOW)
    )
    p = await ProgressService(runtime).progress(uid, weeks=1, now=NOW)
    assert [(t.name, t.due, t.learning) for t in p.tracks] == [("DSA", 1, 0)]
