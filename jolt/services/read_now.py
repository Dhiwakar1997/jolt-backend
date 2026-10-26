"""Read now: extract a source immediately on the user's own key (BE-11).

`POST /v1/sources/{id}/read-now` does in one request what the nightly sync does
for a source: the original pages go to the user's model provider (images as
image inputs, PDFs as document inputs), and the answer is stored through the
**same path** as `jolt_store_extraction`: `SyncService.store_extraction`
(one active read per source; the previous read becomes `superseded`; a pending
read-again request made before this read started is answered), then
`ReconcileService` with the read's concepts (new concepts on a first read;
unchanged / refined / changed / removed / new against the previous read on a
re-read), run to completion, then `SyncService.create_questions` for each new
or successor concept. The read's `model_id` is the model the provider says
answered; `client_label` is "Read now" and `via` is `read_now`.

The read is recorded on the read itself (`Extraction.via`, `provider`) and in
the user's daily usage, not on a sync run: a sync run is a nightly client's
record (sync health, first sync, "New since last sync"), and read now is the
learner's own action, whose result the app shows from the response.

While the call runs the source is held under a lease (`in_flight`), like a
sync claim, so a nightly sync or a second read now cannot read it at the same
time (`source_busy`). On any failure before the read is stored, the source goes
back to its previous status, so the nightly sync still reads it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from jolt.credentials.inference import call_model, resolve_provider
from jolt.credentials.prompts import READ_SYSTEM, read_context
from jolt.credentials.providers import (
    PROVIDER_ERROR,
    FilePart,
    ModelCallError,
    ModelRequest,
    TextPart,
    parse_json_answer,
)
from jolt.domain.models import ProcessingStatus, Source, Track
from jolt.runtime import Runtime
from jolt.services.ownership import NotOwned
from jolt.services.reconcile import ReconcileError, ReconcileService
from jolt.services.spend import SpendGuard, UsageView
from jolt.services.sync import QuestionInput, SyncError, SyncService

READ_NOW_LABEL = "Read now"
VIA_READ_NOW = "read_now"

SOURCE_BUSY = "source_busy"
SOURCE_TOO_LARGE = "source_too_large"
UNSUPPORTED_PAGE = "unsupported_page_type"
TRACK_REQUIRED = "track_required"
SOURCE_UNREADABLE = "source_unreadable"

# Per read: the same bounds as one jolt_store_extraction call (S-07).
MAX_CONCEPTS = 200
MAX_QUESTIONS_PER_CONCEPT = 3
MARKDOWN_MAX_CHARS = 500_000
TEXT_MAX_CHARS = 20_000
AGENDA_MAX_ITEMS = 300
EXISTING_MAX_TITLES = 100

# Formats both providers take as image input; PDFs go in as documents. HEIC is
# accepted at upload but not by either provider (no server-side conversion).
_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
_PDF = "application/pdf"


def sniff(data: bytes) -> Optional[str]:
    """The real type of an uploaded page from its first bytes (S-14: the
    uploader chooses the declared type), or None if it is none we send."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"%PDF-"):
        return _PDF
    return None


def _declared(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


@dataclass
class ReadConcept:
    concept_id: str
    title: str
    change: str
    questions_written: int = 0


@dataclass
class ReadNowResult:
    source_id: str
    extraction_id: str
    provider: str
    model_id: Optional[str]
    confidence: float
    low_confidence: bool
    reconciliation_status: str
    concepts: list[ReadConcept] = field(default_factory=list)
    removed_concept_ids: list[str] = field(default_factory=list)
    questions_written: int = 0
    usage: Optional[UsageView] = None
    input_tokens: int = 0
    output_tokens: int = 0


def _questions(raw) -> list[QuestionInput]:
    """The model's questions that follow the rules: a stem, an expected answer,
    exactly four distinct options and a correct index among them."""
    out: list[QuestionInput] = []
    for q in raw if isinstance(raw, list) else []:
        if not isinstance(q, dict):
            continue
        stem = str(q.get("stem") or "").strip()
        expected = str(q.get("expected_answer") or "").strip()
        options = q.get("options")
        index = q.get("correct_index")
        if not stem or not expected or not isinstance(options, list) or len(options) != 4:
            continue
        options = [str(o).strip() for o in options]
        if not all(options) or not isinstance(index, int) or not 0 <= index < 4:
            continue
        out.append(
            QuestionInput(
                stem=stem[:TEXT_MAX_CHARS],
                options=[o[:TEXT_MAX_CHARS] for o in options],
                correct_index=index,
                expected_answer=expected[:TEXT_MAX_CHARS],
            )
        )
        if len(out) == MAX_QUESTIONS_PER_CONCEPT:
            break
    return out


class ReadNowService:
    def __init__(self, rt: Runtime) -> None:
        self._rt = rt

    # -- checks ---------------------------------------------------------------
    def _check_pages(self, source: Source) -> None:
        s = self._rt.settings
        total = 0
        for page in source.page_list():
            kind = _declared(page.content_type)
            number = page.index + 1
            if kind != _PDF and kind not in _IMAGE_TYPES:
                raise ModelCallError(
                    UNSUPPORTED_PAGE,
                    f"page {number} is {kind or 'of an unknown type'}, which model providers "
                    "do not read; it will be read in tonight's sync",
                    status=415,
                )
            size = page.size_bytes if page.size_bytes is not None else (page.declared_size or 0)
            if kind in _IMAGE_TYPES and size > s.read_now_max_image_bytes:
                raise ModelCallError(
                    SOURCE_TOO_LARGE,
                    f"page {number} is larger than {s.read_now_max_image_bytes:,} bytes, the "
                    "most read now sends per image; it will be read in tonight's sync",
                    status=413,
                )
            total += size
        if total > s.read_now_max_total_bytes:
            raise ModelCallError(
                SOURCE_TOO_LARGE,
                f"the note is larger than {s.read_now_max_total_bytes:,} bytes, the most read "
                "now sends at once; it will be read in tonight's sync",
                status=413,
            )

    @staticmethod
    def _busy(source: Source) -> bool:
        now = datetime.now(timezone.utc)
        return (
            source.processing_status == ProcessingStatus.IN_FLIGHT
            and source.lease_expires_at is not None
            and source.lease_expires_at > now
        )

    # -- lease ----------------------------------------------------------------
    async def _claim(self, source: Source) -> tuple[Source, ProcessingStatus]:
        previous = source.processing_status
        if previous == ProcessingStatus.IN_FLIGHT:  # a lapsed lease: back to the queue
            previous = ProcessingStatus.UNPROCESSED
        now = datetime.now(timezone.utc)
        source.processing_status = ProcessingStatus.IN_FLIGHT
        source.lease_expires_at = now + timedelta(seconds=self._rt.settings.lease_ttl_seconds)
        source.claimed_at = now
        saved = await self._rt.repos.sources.replace(source, etag=source.etag)
        if saved is None:
            raise ModelCallError(
                SOURCE_BUSY, "this note is being read right now; try again in a moment", status=409
            )
        return saved, previous

    async def _release(self, user_id: str, held: Source, previous: ProcessingStatus) -> None:
        """Give the source back as it was (our lease only)."""
        for _ in range(5):
            source = await self._rt.repos.sources.get_source(held.id, user_id)
            if (
                source is None
                or source.processing_status != ProcessingStatus.IN_FLIGHT
                or source.claimed_at != held.claimed_at
            ):
                return
            source.processing_status = previous
            source.lease_expires_at = None
            if await self._rt.repos.sources.replace(source, etag=source.etag) is not None:
                return

    # -- context ----------------------------------------------------------------
    async def _track(self, user_id: str, source: Source) -> Optional[Track]:
        if not source.track_id:
            return None
        return await self._rt.repos.tracks.get_track(source.track_id, user_id)

    async def _context(
        self, user_id: str, source: Source, track: Optional[Track], own_tracks: list[Track]
    ) -> str:
        reread = source.active_extraction_id is not None
        agenda: list[tuple[str, str]] = []
        existing: list[str] = []
        if track is not None:
            agenda = [(i.concept_key, i.label) for i in track.agenda.syllabus][:AGENDA_MAX_ITEMS]
            if not reread:
                concepts = await self._rt.repos.concepts.list_for_track(track.id)
                existing = [c.title or c.text[:80] for c in concepts if c.source_id != source.id][
                    :EXISTING_MAX_TITLES
                ]
        return read_context(
            title=source.title or source.filename,
            page_count=len(source.page_list()),
            track_name=track.name if track else None,
            agenda=agenda,
            existing_titles=existing,
            reread=reread,
            tracks=None if track else [(t.id, t.name) for t in own_tracks],
        )

    async def _pages(self, source: Source) -> list[FilePart]:
        s = self._rt.settings
        parts: list[FilePart] = []
        for page in source.page_list():
            data = await self._rt.blob.read_bytes(page.blob_path, s.read_now_max_total_bytes)
            if data is None:
                raise ModelCallError(
                    SOURCE_UNREADABLE,
                    f"page {page.index + 1} could not be read from storage",
                    status=409,
                )
            kind = sniff(data)
            if kind is None:
                raise ModelCallError(
                    UNSUPPORTED_PAGE,
                    f"page {page.index + 1} is not a JPEG, PNG, WebP or PDF file",
                    status=415,
                )
            if kind in _IMAGE_TYPES and len(data) > s.read_now_max_image_bytes:
                raise ModelCallError(
                    SOURCE_TOO_LARGE,
                    f"page {page.index + 1} is larger than {s.read_now_max_image_bytes:,} bytes",
                    status=413,
                )
            parts.append(FilePart(media_type=kind, data=data, filename=page.filename))
        return parts

    # -- the read ---------------------------------------------------------------
    async def read_now(
        self, user_id: str, source_id: str, *, provider: Optional[str] = None
    ) -> ReadNowResult:
        source = await self._rt.repos.sources.get_source(source_id, user_id)
        if source is None or source.confirmed_at is None:
            raise NotOwned("source not found")
        chosen = await resolve_provider(self._rt, user_id, provider)
        if self._busy(source):
            raise ModelCallError(
                SOURCE_BUSY,
                "this note is being read right now (a sync or another read now); try again "
                "in a moment",
                status=409,
            )
        self._check_pages(source)
        track = await self._track(user_id, source)
        own_tracks = [] if track else await self._rt.repos.tracks.list_for_user(user_id)
        if track is None and not own_tracks:
            raise ModelCallError(
                TRACK_REQUIRED, "create a track first; concepts need a track", status=409
            )
        await SpendGuard(self._rt).check(user_id)

        held, previous = await self._claim(source)
        stored = False
        try:
            parts: list = await self._pages(held)
            parts.append(TextPart(await self._context(user_id, held, track, own_tracks)))
            request = ModelRequest(
                system=READ_SYSTEM,
                parts=parts,
                max_tokens=self._rt.settings.model_read_max_tokens,
                effort="medium",
                timeout=self._rt.settings.model_read_timeout_seconds,
            )
            call = await call_model(self._rt, user_id, chosen, "read_now", request)
            answer = parse_json_answer(call.response.text)
            if answer is None:
                detail = (
                    "the note was too long to read in one go; it will be read in tonight's sync"
                    if call.response.truncated
                    else "the model's answer could not be read; try again"
                )
                raise ModelCallError(PROVIDER_ERROR, detail)
            markdown = str(answer.get("markdown") or "").strip()[:MARKDOWN_MAX_CHARS]
            if not markdown:
                raise ModelCallError(PROVIDER_ERROR, "the model returned an empty read; try again")
            confidence = answer.get("confidence")
            confidence = (
                min(1.0, max(0.0, float(confidence)))
                if isinstance(confidence, (int, float))
                else 0.0
            )
            concepts, questions = self._concepts(answer.get("concepts"), track, own_tracks)

            reconcile = ReconcileService(self._rt)
            try:
                proposed = await reconcile.prepare(user_id, held, concepts)
            except (ReconcileError, NotOwned) as exc:
                raise ModelCallError(
                    PROVIDER_ERROR, "the model's concepts could not be used; try again"
                ) from exc
            sync = SyncService(self._rt)
            extraction = await sync.store_extraction(
                user_id,
                held.id,
                markdown,
                confidence,
                call.response.model_id,
                supersede=held.active_extraction_id is not None,
                client_label=READ_NOW_LABEL,
                via=VIA_READ_NOW,
                provider=call.provider,
            )
            stored = True
        finally:
            if not stored:
                await self._release(user_id, held, previous)

        current = await self._rt.repos.sources.get_source(held.id, user_id)
        view = await reconcile.start(user_id, current or held, extraction, proposed)
        for _ in range(len(proposed) + 2):  # one request: run the checkpoint to the end
            if view.status == "complete":
                break
            view = await reconcile.resume(user_id, held.id)

        result = ReadNowResult(
            source_id=held.id,
            extraction_id=extraction.id,
            provider=call.provider,
            model_id=call.response.model_id,
            confidence=confidence,
            low_confidence=confidence < self._rt.settings.low_confidence_threshold,
            reconciliation_status=view.status,
            usage=call.usage,
            input_tokens=call.response.input_tokens,
            output_tokens=call.response.output_tokens,
        )
        for delta in view.deltas:
            if delta.change == "removed":
                if delta.existing_concept_id:
                    result.removed_concept_ids.append(delta.existing_concept_id)
                continue
            if not delta.concept_id or delta.index is None:
                continue
            item = ReadConcept(
                concept_id=delta.concept_id,
                title=(proposed[delta.index].title or proposed[delta.index].text[:80]),
                change=delta.change,
            )
            wanted = questions[delta.index]
            if delta.applied and delta.change in ("new", "changed") and wanted:
                try:
                    created = await sync.create_questions(user_id, delta.concept_id, wanted)
                except SyncError:
                    created = []
                item.questions_written = len(created)
                result.questions_written += len(created)
            result.concepts.append(item)
        return result

    def _concepts(
        self, raw, track: Optional[Track], own_tracks: list[Track]
    ) -> tuple[list[dict], list[list[QuestionInput]]]:
        """The model's concepts as `jolt_store_extraction` takes them, with their
        questions by the same index. Concepts without text, or (for a note with
        no track) without one of the user's tracks, are left out."""
        track_ids = {t.id for t in own_tracks}
        concepts: list[dict] = []
        questions: list[list[QuestionInput]] = []
        for c in raw if isinstance(raw, list) else []:
            if not isinstance(c, dict):
                continue
            text = str(c.get("text") or "").strip()[:TEXT_MAX_CHARS]
            if not text:
                continue
            if track is not None:
                track_id = track.id
            else:
                track_id = str(c.get("track_id") or "")
                if track_id not in track_ids:
                    if len(track_ids) != 1:
                        continue
                    track_id = next(iter(track_ids))
            key = str(c.get("concept_key") or "").strip()[:200] or None
            page = c.get("page")
            concepts.append(
                {
                    "text": text,
                    "title": str(c.get("title") or "").strip()[:120] or None,
                    "concept_key": key,
                    "page": page if isinstance(page, int) and page >= 1 else None,
                    "track_id": track_id,
                }
            )
            questions.append(_questions(c.get("questions")))
            if len(concepts) == MAX_CONCEPTS:
                break
        return concepts, questions
