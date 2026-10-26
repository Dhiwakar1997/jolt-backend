"""Upsert the v1 DSA content as the curated `dsa` track (BE-09, decisions D2, D7).

What gets written, all in the existing containers (D7):

* `tracks` (curated bucket): the track — `origin: jolt`, `user_id: null`,
  `kind: dsa_patterns`, agenda `locked` with the 24 patterns as its syllabus —
  and its track content document (`<track_id>.content`: the decision tree, the
  pattern order and the problem index the session draw reads).
* `concepts` (partition `dsa`): one concept per pattern, `curated: true`,
  carrying its library entry.
* `questions` (partition: the pattern's concept): one `pattern_match` question
  per playable problem.

Ids are derived from stable keys (the pattern's slug, the problem's
`leetcodeId`), and every document is compared with what is stored before it is
written, so re-running the import with the same input writes nothing. A problem
whose `algorithm` changed moves to its new pattern: the old document is retired
(kept, so past reviews still resolve) and a new one is written under the new
concept. Problems that disappear from the input stay playable unless `prune`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from jolt.curated.dsa_source import DsaBundle, Problem, pattern_slug
from jolt.domain.models import (
    Agenda,
    AgendaSource,
    AgendaStatus,
    Concept,
    PatternLibrary,
    PatternMatchContent,
    ProblemExample,
    ProblemIndexEntry,
    Question,
    QuestionStatus,
    QuestionType,
    SyllabusItem,
    Track,
    TrackContent,
    TrackKind,
    TrackOrigin,
)
from jolt.runtime import Runtime

DSA_TRACK_ID = "dsa"
DSA_TRACK_NAME = "Data structures and algorithms"
DSA_TRACK_DESCRIPTION = (
    "The 24 patterns behind most interview problems. Read a problem, name the pattern, "
    "and Jolt brings each one back before you forget it."
)

# Fields that are bookkeeping, not content: never compared, never rewritten.
_VOLATILE = {"etag", "created_at", "imported_at", "retired_at", "content_hash"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def concept_id_for(track_id: str, pattern: str) -> str:
    return hashlib.sha256(
        f"curated:{track_id}:pattern:{pattern_slug(pattern)}".encode()
    ).hexdigest()[:32]


def question_id_for(track_id: str, leetcode_id: int) -> str:
    return hashlib.sha256(f"curated:{track_id}:problem:{leetcode_id}".encode()).hexdigest()[:32]


def _content(model: Any) -> dict:
    return {k: v for k, v in model.model_dump(mode="json").items() if k not in _VOLATILE}


@dataclass
class Tally:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    retired: int = 0
    moved: int = 0

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class ImportReport:
    track_id: str
    dry_run: bool
    batches: list[str]
    playable: int
    skipped: int
    warnings: int
    review: int
    track: str = "unchanged"  # created | updated | unchanged
    content: str = "unchanged"
    concepts: Tally = field(default_factory=Tally)
    questions: Tally = field(default_factory=Tally)
    by_pattern: dict[str, int] = field(default_factory=dict)
    by_difficulty: dict[str, int] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return (
            self.track != "unchanged"
            or self.content != "unchanged"
            or any(v for k, v in self.concepts.as_dict().items() if k != "unchanged")
            or any(v for k, v in self.questions.as_dict().items() if k != "unchanged")
        )

    def as_dict(self) -> dict:
        return {
            "track_id": self.track_id,
            "dry_run": self.dry_run,
            "batches": self.batches,
            "playable": self.playable,
            "skipped_not_playable": self.skipped,
            "warnings": self.warnings,
            "review": self.review,
            "track": self.track,
            "content": self.content,
            "concepts": self.concepts.as_dict(),
            "questions": self.questions.as_dict(),
            "by_difficulty": self.by_difficulty,
            "by_pattern": self.by_pattern,
            "changed": self.changed,
        }


class ImportRefused(Exception):
    """The input failed validation; nothing was written."""


def _library(entry: dict, pattern: str, animated: set[str]) -> PatternLibrary:
    return PatternLibrary(
        category=str(entry.get("category") or ""),
        time=str(entry.get("time") or ""),
        space=str(entry.get("space") or ""),
        when_to_reach=str(entry.get("whenToReach") or ""),
        tells=[str(t) for t in entry.get("tells") or []],
        pseudocode=[str(t) for t in entry.get("pseudocode") or []],
        explanation=[str(t) for t in entry.get("explanation") or []],
        animation_id=pattern_slug(pattern) if pattern in animated else None,
    )


class DsaImporter:
    def __init__(self, rt: Runtime, track_id: str = DSA_TRACK_ID) -> None:
        self._rt = rt
        self._track_id = track_id

    # -- desired documents ---------------------------------------------------
    def _track(self, bundle: DsaBundle) -> Track:
        return Track(
            id=self._track_id,
            user_id=None,
            name=DSA_TRACK_NAME,
            origin=TrackOrigin.JOLT,
            kind=TrackKind.DSA_PATTERNS,
            description=DSA_TRACK_DESCRIPTION,
            agenda=Agenda(
                status=AgendaStatus.LOCKED,
                source=AgendaSource.AGENT,
                syllabus=[
                    SyllabusItem(concept_key=pattern_slug(p), label=p, source=AgendaSource.AGENT)
                    for p in bundle.patterns
                ],
            ),
        )

    def _concept(self, bundle: DsaBundle, pattern: str) -> Concept:
        entry = bundle.library[pattern]
        return Concept(
            id=concept_id_for(self._track_id, pattern),
            track_id=self._track_id,
            text=str(entry.get("whenToReach") or pattern),
            title=pattern,
            syllabus_ref=pattern_slug(pattern),
            curated=True,
            library=_library(entry, pattern, bundle.animations),
        )

    def _question(self, bundle: DsaBundle, p: Problem) -> Question:
        options = list(bundle.patterns)
        return Question(
            id=question_id_for(self._track_id, p.leetcode_id),
            concept_id=concept_id_for(self._track_id, p.algorithm),
            type=QuestionType.PATTERN_MATCH,
            stem=p.description,
            options=options,
            correct_index=options.index(p.algorithm),
            acceptable_indexes=[options.index(a) for a in p.acceptable if a in options],
            expected_answer=p.algorithm,
            title=p.title,
            pattern_match=PatternMatchContent(
                track_id=self._track_id,
                leetcode_id=p.leetcode_id,
                leetcode_url=p.url,
                description=p.description,
                io_input=p.io_input,
                io_output=p.io_output,
                examples=[ProblemExample(**e) for e in p.examples],
                difficulty=p.difficulty,
                time_weight=p.time_weight,
                near_duplicate_of=p.near_duplicate_of,
                batch=p.batch,
            ),
        )

    # -- run -------------------------------------------------------------------
    async def run(
        self, bundle: DsaBundle, *, dry_run: bool = False, prune: bool = False
    ) -> ImportReport:
        if not bundle.ok:
            raise ImportRefused(
                f"{len(bundle.errors)} validation error(s); first: {bundle.errors[0]}"
            )
        repos = self._rt.repos
        report = ImportReport(
            track_id=self._track_id,
            dry_run=dry_run,
            batches=bundle.batches,
            playable=len(bundle.problems),
            skipped=bundle.skipped,
            warnings=len(bundle.warnings),
            review=len(bundle.review),
        )
        for p in bundle.problems:
            report.by_pattern[p.algorithm] = report.by_pattern.get(p.algorithm, 0) + 1
            report.by_difficulty[p.difficulty] = report.by_difficulty.get(p.difficulty, 0) + 1

        # Track.
        desired_track = self._track(bundle)
        stored_track = await repos.tracks.get_track(self._track_id, None)
        if stored_track is None:
            report.track = "created"
            if not dry_run:
                await repos.tracks.create(desired_track)
        elif _content(stored_track) != _content(desired_track):
            report.track = "updated"
            if not dry_run:
                desired_track.created_at = stored_track.created_at
                await repos.tracks.replace(desired_track, etag=stored_track.etag)

        # Concepts (one per pattern).
        for pattern in bundle.patterns:
            desired = self._concept(bundle, pattern)
            stored = await repos.concepts.get_concept(desired.id, self._track_id)
            if stored is None:
                report.concepts.created += 1
                if not dry_run:
                    await repos.concepts.create(desired)
            elif _content(stored) != _content(desired):
                report.concepts.updated += 1
                if not dry_run:
                    desired.created_at = stored.created_at
                    await repos.concepts.replace(desired, etag=stored.etag)
            else:
                report.concepts.unchanged += 1

        # Questions (one per playable problem), upserted by leetcodeId.
        content = await repos.track_content.get_content(self._track_id)
        previous = {e.leetcode_id: e for e in (content.problems if content else [])}
        index: dict[int, ProblemIndexEntry] = {}
        for p in bundle.problems:
            desired = self._question(bundle, p)
            old = previous.get(p.leetcode_id)
            if old is not None and old.concept_id != desired.concept_id:
                # Retagged: retire the document under the old pattern.
                report.questions.moved += 1
                if not dry_run:
                    await self._retire(old.question_id, old.concept_id)
            stored = await repos.questions.get_question(desired.id, desired.concept_id)
            if stored is None:
                report.questions.created += 1
                if not dry_run:
                    await repos.questions.create(desired)
            elif _content(stored) != _content(desired):
                report.questions.updated += 1
                if not dry_run:
                    desired.created_at = stored.created_at
                    await repos.questions.replace(desired, etag=stored.etag)
            else:
                report.questions.unchanged += 1
            index[p.leetcode_id] = ProblemIndexEntry(
                question_id=desired.id,
                concept_id=desired.concept_id,
                leetcode_id=p.leetcode_id,
                difficulty=p.difficulty,
                time_weight=p.time_weight,
                near_duplicate_of=p.near_duplicate_of,
            )
        for lc, old in previous.items():
            if lc in index:
                continue
            if prune:
                report.questions.retired += 1
                if not dry_run:
                    await self._retire(old.question_id, old.concept_id)
            else:
                index[lc] = old  # still playable: an import without --include-new keeps it

        # Track content: decision tree, pattern order, problem index.
        desired_content = TrackContent(
            id=TrackContent.id_for(self._track_id),
            track_id=self._track_id,
            kind=TrackKind.DSA_PATTERNS,
            patterns=list(bundle.patterns),
            decision_tree=bundle.decision_tree,
            problems=[index[k] for k in sorted(index)],
        )
        digest = hashlib.sha256(
            json.dumps(_content(desired_content), sort_keys=True).encode()
        ).hexdigest()
        if content is None or content.content_hash != digest:
            report.content = "created" if content is None else "updated"
            if not dry_run:
                desired_content.content_hash = digest
                desired_content.imported_at = _now()
                if content is None:
                    await repos.track_content.create(desired_content)
                else:
                    desired_content.created_at = content.created_at
                    await repos.track_content.replace(desired_content, etag=content.etag)
        return report

    async def _retire(self, question_id: str, concept_id: str) -> None:
        q: Optional[Question] = await self._rt.repos.questions.get_question(question_id, concept_id)
        if q is None or q.status == QuestionStatus.RETIRED:
            return
        q.status = QuestionStatus.RETIRED
        q.retired_at = _now()
        await self._rt.repos.questions.replace(q, etag=q.etag)


async def import_dsa(
    rt: Runtime,
    bundle: DsaBundle,
    *,
    dry_run: bool = False,
    prune: bool = False,
    track_id: str = DSA_TRACK_ID,
) -> ImportReport:
    return await DsaImporter(rt, track_id).run(bundle, dry_run=dry_run, prune=prune)


__all__ = [
    "DSA_TRACK_ID",
    "DsaImporter",
    "ImportRefused",
    "ImportReport",
    "concept_id_for",
    "import_dsa",
    "question_id_for",
]
