"""Blob Storage access — signed URL generation, existence/size checks (LLD §6, §7.1).

Jolt compute never streams the file bytes: the app PUTs straight to Blob against a
short-TTL SAS URL, and agents GET the source via a signed read URL. This module
only mints those URLs and verifies uploads.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

from azure.storage.blob import BlobSasPermissions, generate_blob_sas
from azure.storage.blob.aio import BlobServiceClient

from jolt.config import Settings, get_settings


@dataclass
class BlobGateway:
    service: BlobServiceClient
    container: str
    settings: Settings
    # Account name + key are needed to sign SAS URLs; absent under pure MI, where
    # a user-delegation key is used instead.
    account_key: Optional[str]

    async def close(self) -> None:
        await self.service.close()

    def blob_path(self, user_id: str, source_id: str, filename: str, page: int = 0) -> str:
        """Namespace every blob under the owning user to keep the store legible."""
        return page_blob_path(user_id, source_id, filename, page)

    async def _sas(self, blob_path: str, permission: BlobSasPermissions) -> str:
        expiry = datetime.now(timezone.utc) + timedelta(seconds=self.settings.blob_sas_ttl_seconds)
        account_name = self.service.account_name
        if self.account_key:
            token = generate_blob_sas(
                account_name=account_name,
                container_name=self.container,
                blob_name=blob_path,
                account_key=self.account_key,
                permission=permission,
                expiry=expiry,
            )
        else:
            # Managed Identity path: sign with a user-delegation key.
            start = datetime.now(timezone.utc) - timedelta(minutes=5)
            udk = await self.service.get_user_delegation_key(start, expiry)
            token = generate_blob_sas(
                account_name=account_name,
                container_name=self.container,
                blob_name=blob_path,
                user_delegation_key=udk,
                permission=permission,
                expiry=expiry,
                start=start,
            )
        base = self.service.url.rstrip("/")
        return f"{base}/{self.container}/{quote(blob_path)}?{token}"

    async def upload_url(self, blob_path: str) -> str:
        """Short-TTL PUT url scoped to exactly one blob path (LLD §7.1)."""
        return await self._sas(blob_path, BlobSasPermissions(create=True, write=True))

    async def read_url(self, blob_path: str) -> str:
        """Short-TTL GET url for an agent to read the original source (LLD §7.2)."""
        return await self._sas(blob_path, BlobSasPermissions(read=True))

    async def stat(self, blob_path: str) -> Optional[dict]:
        """Return {size} if the blob exists, else None (upload confirm check)."""
        blob = self.service.get_blob_client(self.container, blob_path)
        try:
            props = await blob.get_blob_properties()
        except Exception:
            return None
        return {"size": props.size, "content_type": props.content_settings.content_type}

    async def sha256(self, blob_path: str) -> Optional[str]:
        """Hex SHA-256 of the stored bytes, or None if the blob is missing.

        Blob has no server-side SHA-256, so confirm streams the bytes once to
        check the hash the client declared (BE-05). Callers bound the size first
        (`stat`), so this never reads more than the per-file upload cap.
        """
        blob = self.service.get_blob_client(self.container, blob_path)
        try:
            downloader = await blob.download_blob()
        except Exception:
            return None
        digest = hashlib.sha256()
        async for chunk in downloader.chunks():
            digest.update(chunk)
        return digest.hexdigest()

    async def delete(self, blob_path: str) -> None:
        """Delete a blob; missing is fine (used to drop a rejected upload)."""
        blob = self.service.get_blob_client(self.container, blob_path)
        try:
            await blob.delete_blob()
        except Exception:
            pass


_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def page_blob_path(user_id: str, source_id: str, filename: str, page: int = 0) -> str:
    """`<user>/<source>/<page>-<filename>`, the filename reduced to safe characters.

    The page index keeps two files with the same name (phones often name every
    photo `image.jpg`) apart, and the reduced name keeps paths free of `/`, `..`
    and characters that need escaping in a SAS URL.
    """
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    safe = _UNSAFE.sub("_", base).strip("._") or "file"
    return f"{user_id}/{source_id}/{page}-{safe[:120]}"


async def init_blob(settings: Settings | None = None) -> BlobGateway:
    settings = settings or get_settings()
    account_key: Optional[str] = None

    if settings.blob_connection_string:
        service = BlobServiceClient.from_connection_string(settings.blob_connection_string)
        # Extract the key from the connection string so SAS signing works locally.
        for part in settings.blob_connection_string.split(";"):
            if part.startswith("AccountKey="):
                account_key = part[len("AccountKey=") :]
    else:
        if not settings.blob_account_url:
            raise RuntimeError(
                "BLOB_ACCOUNT_URL is not set. Fill it in .env after deploying Storage."
            )
        if settings.use_managed_identity:
            from azure.identity.aio import DefaultAzureCredential

            service = BlobServiceClient(
                account_url=settings.blob_account_url, credential=DefaultAzureCredential()
            )
        else:
            if not settings.blob_account_key:
                raise RuntimeError(
                    "AUTH_MODE=key but BLOB_ACCOUNT_KEY (or BLOB_CONNECTION_STRING) is empty."
                )
            service = BlobServiceClient(
                account_url=settings.blob_account_url, credential=settings.blob_account_key
            )
            account_key = settings.blob_account_key

    # Ensure the container exists.
    try:
        await service.create_container(settings.blob_container)
    except Exception:
        pass  # already exists

    return BlobGateway(
        service=service,
        container=settings.blob_container,
        settings=settings,
        account_key=account_key,
    )
