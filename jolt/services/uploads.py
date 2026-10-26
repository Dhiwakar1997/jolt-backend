"""Direct-upload lifecycle (LLD §7.1, BE-05).

Jolt compute never handles the upload: it mints one short-TTL SAS PUT url per
file, the client uploads straight to Blob, and confirm checks every file. A
multi-page note is **one source with N pages** (decision A7). The source sits
`unprocessed` until a sync claims it.

Confirm checks, per page: the blob exists, its size is within the per-file cap
and equals the size declared at request time, and the SHA-256 of the stored
bytes equals the declared hash. Blob computes no SHA-256 itself, so confirm
streams each file once to hash it; the size check runs first, so it never reads
more than `UPLOAD_MAX_BYTES` per page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from jolt.domain.models import ProcessingStatus, Source, SourcePage
from jolt.runtime import Runtime

# What the app may upload: phone photos (JPEG, HEIC), screenshots (PNG, WebP), PDFs.
ACCEPTED_CONTENT_TYPES: tuple[str, ...] = (
    "image/jpeg",
    "image/png",
    "image/heic",
    "image/webp",
    "application/pdf",
)

_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass
class PageSpec:
    filename: str
    content_type: str
    sha256: str
    size: Optional[int] = None  # declared bytes; required for multi-page uploads


@dataclass
class PageTicket:
    index: int
    upload_url: str


@dataclass
class UploadTicket:
    source_id: str
    upload_url: str  # the first page's URL (single-file clients)
    pages: list[PageTicket] = field(default_factory=list)
    max_bytes_per_file: int = 0
    accepted_types: list[str] = field(default_factory=list)


@dataclass
class ConfirmPage:
    index: int
    sha256: str
    size: int


class UploadError(Exception):
    """Confirm failed (400)."""


class UploadInvalid(Exception):
    """The upload request breaks a limit (422). `loc` points at the field."""

    def __init__(self, loc: tuple, msg: str) -> None:
        super().__init__(msg)
        self.loc = loc
        self.msg = msg


class UploadTrackNotFound(Exception):
    """`track_id` is not one of the caller's tracks (404)."""


def normalise_content_type(value: str) -> str:
    """`Image/JPEG; charset=x` -> `image/jpeg`."""
    return value.split(";", 1)[0].strip().lower()


class UploadService:
    def __init__(self, rt: Runtime) -> None:
        self._rt = rt

    @property
    def max_bytes(self) -> int:
        return self._rt.settings.upload_max_bytes

    def _validate(self, pages: list[PageSpec], loc_prefix: tuple) -> list[PageSpec]:
        settings = self._rt.settings
        if not pages:
            raise UploadInvalid(loc_prefix, "at least one file is required")
        if len(pages) > settings.upload_max_pages:
            raise UploadInvalid(loc_prefix, f"at most {settings.upload_max_pages} files per upload")
        out: list[PageSpec] = []
        for i, p in enumerate(pages):
            loc = (*loc_prefix, i) if len(loc_prefix) and loc_prefix[-1] == "pages" else loc_prefix
            ctype = normalise_content_type(p.content_type)
            if ctype not in ACCEPTED_CONTENT_TYPES:
                raise UploadInvalid(
                    (*loc, "content_type"),
                    f"unsupported content type {p.content_type!r}; accepted: "
                    + ", ".join(ACCEPTED_CONTENT_TYPES),
                )
            if not _SHA256.match(p.sha256 or ""):
                raise UploadInvalid((*loc, "sha256"), "sha256 must be 64 hex characters")
            if p.size is not None and p.size > settings.upload_max_bytes:
                raise UploadInvalid(
                    (*loc, "size"), f"file is larger than {settings.upload_max_bytes} bytes"
                )
            if not p.filename or not p.filename.strip():
                raise UploadInvalid((*loc, "filename"), "filename is required")
            out.append(
                PageSpec(
                    filename=p.filename.strip(),
                    content_type=ctype,
                    sha256=p.sha256.lower(),
                    size=p.size,
                )
            )
        return out

    async def request_upload(
        self,
        user_id: str,
        pages: list[PageSpec],
        *,
        track_id: Optional[str] = None,
        title: Optional[str] = None,
        loc_prefix: tuple = ("body", "pages"),
    ) -> UploadTicket:
        """Create one source for `pages` and mint one signed PUT URL per page."""
        pages = self._validate(pages, loc_prefix)
        if track_id is not None:
            track = await self._rt.repos.tracks.get_track(track_id, user_id)
            if track is None:
                raise UploadTrackNotFound("track not found")

        first = pages[0]
        source = Source(
            user_id=user_id,
            filename=first.filename,
            content_type=first.content_type,
            sha256=first.sha256,
            blob_path="",  # set below once we have the id
            processing_status=ProcessingStatus.UNPROCESSED,
            track_id=track_id,
            title=(title or "").strip() or first.filename,
        )
        source.pages = [
            SourcePage(
                index=i,
                filename=p.filename,
                content_type=p.content_type,
                sha256=p.sha256,
                blob_path=self._rt.blob.blob_path(user_id, source.id, p.filename, i),
                declared_size=p.size,
            )
            for i, p in enumerate(pages)
        ]
        source.blob_path = source.pages[0].blob_path
        created = await self._rt.repos.sources.create(source)
        tickets = [
            PageTicket(index=p.index, upload_url=await self._rt.blob.upload_url(p.blob_path))
            for p in created.page_list()
        ]
        return UploadTicket(
            source_id=created.id,
            upload_url=tickets[0].upload_url,
            pages=tickets,
            max_bytes_per_file=self.max_bytes,
            accepted_types=list(ACCEPTED_CONTENT_TYPES),
        )

    async def confirm_upload(
        self,
        user_id: str,
        source_id: str,
        sha256: str | None = None,
        pages: list[ConfirmPage] | None = None,
    ) -> Source:
        source = await self._rt.repos.sources.get_source(source_id, user_id)
        if source is None:
            raise UploadError("source not found")
        stored = source.page_list()
        multi = len(stored) > 1

        def where(page: SourcePage) -> str:
            return f" on page {page.index + 1}" if multi else ""

        # What the client says at confirm must agree with what it declared.
        if sha256 and sha256.lower() != stored[0].sha256.lower():
            raise UploadError("sha256 mismatch between request and confirm")
        by_index = {p.index: p for p in stored}
        for claim in pages or []:
            page = by_index.get(claim.index)
            if page is None:
                raise UploadError(f"no page {claim.index} in this upload")
            if claim.sha256.lower() != page.sha256.lower():
                raise UploadError(f"sha256 mismatch between request and confirm{where(page)}")
            if page.declared_size is not None and claim.size != page.declared_size:
                raise UploadError(f"size mismatch between request and confirm{where(page)}")

        if source.confirmed_at is not None:
            return source  # already confirmed: idempotent

        # Then check the bytes that actually arrived.
        total = 0
        for page in stored:
            stat = await self._rt.blob.stat(page.blob_path)
            if stat is None:
                raise UploadError(f"blob not found — upload did not complete{where(page)}")
            size = int(stat["size"])
            if size > self.max_bytes:
                # A SAS URL cannot cap the PUT, so drop an oversize file here.
                await self._rt.blob.delete(page.blob_path)
                raise UploadError(
                    f"file{where(page)} is larger than {self.max_bytes} bytes; upload it again"
                )
            if page.declared_size is not None and size != page.declared_size:
                raise UploadError(
                    f"size mismatch{where(page)}: {size} bytes arrived, "
                    f"{page.declared_size} were declared"
                )
            actual = await self._rt.blob.sha256(page.blob_path)
            if actual is None:
                raise UploadError(f"blob not found — upload did not complete{where(page)}")
            if actual != page.sha256.lower():
                raise UploadError(
                    f"sha256 mismatch{where(page)}: the uploaded bytes differ from the request"
                )
            page.size_bytes = size
            total += size

        if source.pages:
            source.pages = stored
        source.size_bytes = total
        source.confirmed_at = datetime.now(timezone.utc)
        # Stays unprocessed (ready-for-processing); a sync claims it later.
        return await self._rt.repos.sources.replace(source, etag=source.etag) or source
