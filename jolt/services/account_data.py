"""The user's data rights: export everything, delete everything (BE-08 §5, §6).

**Export** (`POST /v1/export`) writes one JSON bundle to Blob under
`exports/<user_id>/` and returns a signed download URL. The bundle holds every
document Jolt keeps for the user, minus secrets and internals (token hashes,
Key Vault references, blob paths, etags). A new export replaces the previous
one; a Blob lifecycle rule expires them (infra/modules/storage.bicep).

**Delete** (`DELETE /v1/me`) removes, in this order: the Key Vault secrets
(provider API keys, deleted and purged), every blob under the user (uploads,
including abandoned ones, and exports), every Cosmos document under the user
(each container, including the track-, concept-, source- and review-partitioned
ones reached through the user's tracks, sources and reviews), the user's tokens,
then the user document. Tokens and the user go last so a delete that fails half
way can be retried with the same app token. Irreversible.

Curated (shared, BE-09) content is not the user's and stays: the curated tracks,
their concepts and questions and the `<track>.content` documents live in the
curated bucket and in track/concept partitions no user track reaches. What the
learner did on a curated track goes with the account: the enrolments (on the
user document), their cards for curated concepts (`concept_states`), their
answers (`reviews`) and their review sessions.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from jolt.credentials.providers import SUPPORTED_PROVIDERS
from jolt.domain.models import DocBase
from jolt.runtime import Runtime

logger = logging.getLogger("jolt.account_data")

EXPORT_FORMAT = "jolt-export"
EXPORT_FORMAT_VERSION = 1
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def export_prefix(user_id: str) -> str:
    return f"exports/{user_id}/"


def _dump(doc: DocBase, *, drop: Iterable[str] = ()) -> dict[str, Any]:
    out = doc.model_dump(mode="json", exclude={"etag", *drop})
    return out


@dataclass
class ExportResult:
    download_url: str
    expires_at: datetime
    bytes: int
    blob_path: str


@dataclass
class DeletionReport:
    """What a delete removed, per store (logged; tests read it)."""

    secrets: int = 0
    blobs: int = 0
    documents: dict[str, int] = field(default_factory=dict)

    def add(self, container: str, count: int) -> None:
        self.documents[container] = self.documents.get(container, 0) + count


class AccountDataService:
    def __init__(self, rt: Runtime) -> None:
        self._rt = rt

    # -- export -----------------------------------------------------------
    async def bundle(self, user_id: str) -> dict[str, Any]:
        """Every document Jolt keeps for the user, as plain JSON."""
        repos = self._rt.repos
        user = await repos.users.get_user(user_id)
        tracks = await repos.tracks.list_for_user(user_id)
        concepts = []
        for track in tracks:
            concepts += await repos.concepts.list_for_track(track.id, active_only=False)
        questions = []
        for concept in concepts:
            questions += await repos.questions.list_for_concept(concept.id, active_only=False)
        reviews = await repos.reviews.created_since(user_id, _EPOCH)
        reviews.sort(key=lambda r: (r.created_at, r.id))
        feedback = []
        for review in reviews:
            feedback += await repos.answer_feedback.for_review(review.id)
        sources = await repos.sources.list_for_user(user_id)
        extractions = []
        for source in sources:
            extractions += await repos.extractions.history_for_source(source.id)
        tokens = await repos.tokens.list_partition(user_id)
        api_keys = await repos.api_keys.list_for_user(user_id)
        review_sessions = await repos.review_sessions.list_partition(user_id)

        def source_view(source) -> dict[str, Any]:
            out = _dump(source, drop={"blob_path"})
            out["pages"] = [
                {k: v for k, v in page.items() if k != "blob_path"} for page in out["pages"]
            ]
            return out

        return {
            "format": EXPORT_FORMAT,
            "format_version": EXPORT_FORMAT_VERSION,
            "exported_at": _now().isoformat(),
            "user": _dump(user, drop={"auth_subject", "retention_days"}) if user else None,
            "tracks": [_dump(t) for t in tracks],
            "concepts": [_dump(c) for c in concepts],
            "questions": [_dump(q) for q in questions],
            "concept_states": [_dump(s) for s in await repos.concept_states.all_for_user(user_id)],
            "reviews": [_dump(r) for r in reviews],
            "answer_feedback": [_dump(f) for f in feedback],
            "review_sessions": [_dump(s) for s in review_sessions],
            "sessions": [_dump(s) for s in await repos.sessions.list_for_user(user_id)],
            "sources": [source_view(s) for s in sources],
            "extractions": [_dump(e) for e in extractions],
            "sync_runs": [_dump(r) for r in await repos.sync_runs.list_partition(user_id)],
            "mcp_tokens": [
                {
                    "id": t.id,
                    "kind": t.kind.value,
                    "label": t.label,
                    "last4": t.last4,
                    "created_at": t.created_at.isoformat(),
                    "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None,
                }
                for t in tokens
            ],
            "api_keys": [
                {
                    "provider": k.provider,
                    "last4": k.last4,
                    "added_at": k.added_at.isoformat(),
                    "last_validated_at": (
                        k.last_validated_at.isoformat() if k.last_validated_at else None
                    ),
                }
                for k in api_keys
            ],
        }

    async def export(self, user_id: str) -> ExportResult:
        bundle = await self.bundle(user_id)
        data = json.dumps(bundle, ensure_ascii=False, indent=1).encode("utf-8")
        now = _now()
        prefix = export_prefix(user_id)
        await self._rt.blob.delete_prefix(prefix)  # one export at a time
        path = f"{prefix}{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}.json"
        filename = f"jolt-export-{now.date().isoformat()}.json"
        await self._rt.blob.upload_bytes(
            path,
            data,
            "application/json",
            content_disposition=f'attachment; filename="{filename}"',
        )
        ttl = self._rt.settings.export_url_ttl_seconds
        url = await self._rt.blob.read_url(path, ttl_seconds=ttl)
        return ExportResult(
            download_url=url,
            expires_at=now + timedelta(seconds=ttl),
            bytes=len(data),
            blob_path=path,
        )

    # -- delete -----------------------------------------------------------
    async def delete_account(self, user_id: str) -> DeletionReport:
        repos = self._rt.repos
        report = DeletionReport()

        # 1. Key Vault: every provider key (the references say which; every
        #    supported provider is tried too, in case a reference was lost).
        refs = await repos.api_keys.list_for_user(user_id)
        providers = {r.provider for r in refs} | set(SUPPORTED_PROVIDERS)
        for provider in sorted(providers):
            await self._rt.vault.delete_key(user_id, provider)
        report.secrets = len(refs)

        # 2. Blob: uploads (all under `<user_id>/`, confirmed or not) and exports.
        report.blobs += await self._rt.blob.delete_prefix(f"{user_id}/")
        report.blobs += await self._rt.blob.delete_prefix(export_prefix(user_id))

        # 3. Documents partitioned by something other than the user, reached
        #    through the user's own documents.
        for source in await repos.sources.list_for_user(user_id):
            report.add("extractions", await repos.extractions.delete_partition(source.id))
        for review in await repos.reviews.list_partition(user_id):
            report.add("answer_feedback", await repos.answer_feedback.delete_partition(review.id))
        for track in await repos.tracks.list_for_user(user_id):
            for concept in await repos.concepts.list_for_track(track.id, active_only=False):
                report.add("questions", await repos.questions.delete_partition(concept.id))
            report.add("concepts", await repos.concepts.delete_partition(track.id))

        # 4. Documents partitioned by the user.
        for name, repo in (
            ("sources", repos.sources),
            ("sessions", repos.sessions),
            ("concept_states", repos.concept_states),
            ("reviews", repos.reviews),
            ("review_sessions", repos.review_sessions),
            ("api_keys", repos.api_keys),
            ("sync_runs", repos.sync_runs),
            ("tracks", repos.tracks),
            ("tokens", repos.tokens),  # last but one: a failed delete can be retried
        ):
            report.add(name, await repo.delete_partition(user_id))

        # 5. The user.
        report.add("users", await repos.users.delete_partition(user_id))
        logger.info(
            "account deleted: user=%s secrets=%d blobs=%d documents=%s",
            user_id,
            report.secrets,
            report.blobs,
            report.documents,
        )
        return report
