"""Direct-upload lifecycle (LLD §7.1).

Jolt compute never touches the bytes: it mints a short-TTL SAS PUT url, the client
uploads straight to Blob, and confirm verifies existence + size + sha256. The
source sits `unprocessed` until a sync claims it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from jolt.domain.models import ProcessingStatus, Source
from jolt.runtime import Runtime


@dataclass
class UploadTicket:
    source_id: str
    upload_url: str


class UploadError(Exception):
    pass


class UploadService:
    def __init__(self, rt: Runtime) -> None:
        self._rt = rt

    async def request_upload(
        self, user_id: str, filename: str, content_type: str, sha256: str
    ) -> UploadTicket:
        source = Source(
            user_id=user_id,
            filename=filename,
            content_type=content_type,
            sha256=sha256,
            blob_path="",  # set below once we have the id
            processing_status=ProcessingStatus.UNPROCESSED,
        )
        source.blob_path = self._rt.blob.blob_path(user_id, source.id, filename)
        created = await self._rt.repos.sources.create(source)
        upload_url = await self._rt.blob.upload_url(created.blob_path)
        return UploadTicket(source_id=created.id, upload_url=upload_url)

    async def confirm_upload(
        self, user_id: str, source_id: str, sha256: str | None = None
    ) -> Source:
        source = await self._rt.repos.sources.get_source(source_id, user_id)
        if source is None:
            raise UploadError("source not found")

        stat = await self._rt.blob.stat(source.blob_path)
        if stat is None:
            raise UploadError("blob not found — upload did not complete")
        if sha256 and sha256 != source.sha256:
            raise UploadError("sha256 mismatch between request and confirm")

        source.size_bytes = stat["size"]
        source.confirmed_at = datetime.now(timezone.utc)
        # Stays unprocessed (ready-for-processing); a sync claims it later.
        return await self._rt.repos.sources.replace(source, etag=source.etag) or source
