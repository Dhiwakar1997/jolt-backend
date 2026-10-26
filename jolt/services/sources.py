"""Source read models for the app: list, detail with past reads, read again,
storage figure (BE-05).

Everything here is single-partition: sources by `user_id`, a source's reads by
`source_id`. A read carries its own counts (`concepts`, `questions_written`,
filled by the sync tools), so a list row costs one point read of the active
extraction and a detail one history query.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from jolt.domain.models import Extraction, ProcessingStatus, Source
from jolt.runtime import Runtime


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SourceNotFound(Exception):
    pass


@dataclass
class RereadState:
    requested_at: datetime
    preferred_model: Optional[str]


@dataclass
class SourceSummary:
    id: str
    filename: str
    content_type: str
    processing_status: str
    active_extraction_id: Optional[str]
    created_at: datetime
    title: str
    track_id: Optional[str]
    track_name: Optional[str]
    kind: str
    page_count: int
    uploaded_at: datetime
    concepts_found: int
    questions_written: int
    low_confidence: bool
    reread_requested: Optional[RereadState]
    read_count: int


@dataclass
class TouchedConcept:
    concept_id: str
    title: str
    is_new: bool


@dataclass
class ReadSummary:
    id: str
    source_id: str
    number: int
    model_id: Optional[str]
    client_label: Optional[str]
    confidence: float
    low_confidence: bool
    status: str
    created_at: datetime
    concepts_found: int
    questions_written: int
    concepts: list[TouchedConcept]


@dataclass
class ActiveRead(ReadSummary):
    markdown: str = ""


@dataclass
class PageView:
    index: int
    url: str
    content_type: str
    size: int


@dataclass
class SourceDetail(SourceSummary):
    pages: list[PageView] = field(default_factory=list)
    active_read: Optional[ActiveRead] = None
    reads: list[ReadSummary] = field(default_factory=list)


@dataclass
class Storage:
    bytes_used: int
    source_count: int
    page_count: int


class SourceService:
    def __init__(self, rt: Runtime) -> None:
        self._rt = rt

    @property
    def _threshold(self) -> float:
        return self._rt.settings.low_confidence_threshold

    async def _track_names(self, user_id: str) -> dict[str, str]:
        tracks = await self._rt.repos.tracks.list_for_user(user_id)
        return {t.id: t.name for t in tracks}

    async def _get_confirmed(self, user_id: str, source_id: str) -> Source:
        source = await self._rt.repos.sources.get_source(source_id, user_id)
        if source is None or source.confirmed_at is None:
            raise SourceNotFound("source not found")
        return source

    def _summary(
        self, source: Source, active: Optional[Extraction], track_names: dict[str, str]
    ) -> SourceSummary:
        pages = source.page_list()
        reread = (
            RereadState(source.reread_requested_at, source.reread_preferred_model)
            if source.reread_requested_at
            else None
        )
        return SourceSummary(
            id=source.id,
            filename=source.filename,
            content_type=source.content_type,
            processing_status=source.processing_status.value,
            active_extraction_id=source.active_extraction_id,
            created_at=source.created_at,
            title=source.title or source.filename,
            track_id=source.track_id,
            track_name=track_names.get(source.track_id) if source.track_id else None,
            kind=source.kind,
            page_count=len(pages),
            uploaded_at=source.uploaded_at,
            concepts_found=len(active.concepts) if active else 0,
            questions_written=active.questions_written if active else 0,
            low_confidence=bool(active) and active.confidence < self._threshold,
            reread_requested=reread,
            # Counted on the source; a document from before BE-05 with a read has 0.
            read_count=max(source.read_count, 1 if source.active_extraction_id else 0),
        )

    async def list_sources(
        self,
        user_id: str,
        *,
        track_id: Optional[str] = None,
        status: Optional[ProcessingStatus] = None,
    ) -> list[SourceSummary]:
        sources = await self._rt.repos.sources.list_confirmed(
            user_id, track_id=track_id, status=status
        )
        names = await self._track_names(user_id)
        out: list[SourceSummary] = []
        for s in sources:
            active = (
                await self._rt.repos.extractions.get_extraction(s.active_extraction_id, s.id)
                if s.active_extraction_id
                else None
            )
            out.append(self._summary(s, active, names))
        return out

    def _read(self, e: Extraction, number: int, active_id: Optional[str]) -> ReadSummary:
        return ReadSummary(
            id=e.id,
            source_id=e.source_id,
            number=number,
            model_id=e.model_id,
            client_label=e.client_label,
            confidence=e.confidence,
            low_confidence=e.confidence < self._threshold,
            # The source's pointer is the authority on which read is active.
            status="active" if e.id == active_id else "superseded",
            created_at=e.created_at,
            concepts_found=len(e.concepts),
            questions_written=e.questions_written,
            concepts=[TouchedConcept(c.concept_id, c.title, c.is_new) for c in e.concepts],
        )

    async def detail(self, user_id: str, source_id: str) -> SourceDetail:
        source = await self._get_confirmed(user_id, source_id)
        history = await self._rt.repos.extractions.history_for_source(source.id)  # newest first
        oldest_first = list(reversed(history))
        reads = [
            self._read(e, i + 1, source.active_extraction_id) for i, e in enumerate(oldest_first)
        ]
        active_ext = next((e for e in history if e.id == source.active_extraction_id), None)
        active_read = None
        if active_ext is not None:
            base = next(r for r in reads if r.id == active_ext.id)
            active_read = ActiveRead(**base.__dict__, markdown=active_ext.markdown)

        pages = [
            PageView(
                index=p.index,
                url=await self._rt.blob.read_url(p.blob_path),
                content_type=p.content_type,
                size=p.size_bytes if p.size_bytes is not None else (p.declared_size or 0),
            )
            for p in source.page_list()
        ]
        summary = self._summary(source, active_ext, await self._track_names(user_id))
        summary.read_count = len(history)  # exact here: the history is already loaded
        return SourceDetail(
            **summary.__dict__,
            pages=pages,
            active_read=active_read,
            reads=list(reversed(reads)),  # newest first
        )

    async def request_reread(
        self, user_id: str, source_id: str, preferred_model: Optional[str]
    ) -> Source:
        """Queue a re-extraction for the next sync (BE-05 §4).

        The source goes back to `unprocessed` (a failed one gets fresh attempts);
        one already claimed by a running sync keeps its lease, and its pending
        extraction does not answer the request (see SyncService.store_extraction).
        The next claim tells the agent to store with `supersede: true`.
        """
        for _ in range(3):
            source = await self._get_confirmed(user_id, source_id)
            source.reread_requested_at = _now()
            source.reread_preferred_model = preferred_model
            if source.processing_status != ProcessingStatus.IN_FLIGHT:
                source.processing_status = ProcessingStatus.UNPROCESSED
                source.lease_expires_at = None
                source.claim_count = 0
                source.failed_reason = None
            saved = await self._rt.repos.sources.replace(source, etag=source.etag)
            if saved is not None:
                return saved
        raise RuntimeError("could not queue the re-read: the source kept changing")

    async def storage(self, user_id: str) -> Storage:
        sources = await self._rt.repos.sources.list_confirmed(user_id)
        return Storage(
            bytes_used=sum(s.size_bytes or 0 for s in sources),
            source_count=len(sources),
            page_count=sum(len(s.page_list()) for s in sources),
        )
