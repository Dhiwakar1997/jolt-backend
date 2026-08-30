"""Process runtime — initialised gateways and repository access.

Built once in the FastAPI lifespan (LLD §4 main.py assembly) and stored on
`app.state`. Repositories are cheap wrappers over the shared Cosmos gateway, so
they are constructed on demand rather than held as singletons.

This is the composition root: it is allowed to touch both `data/` and the domain,
which is why service composition lives above `domain/` (which stays framework- and
Azure-free).
"""

from __future__ import annotations

from dataclasses import dataclass

from jolt.config import Settings, get_settings
from jolt.credentials.vault import VaultClient
from jolt.data.blob import BlobGateway, init_blob
from jolt.data.cosmos import CosmosGateway, init_cosmos
from jolt.data.repositories.concepts import (
    ConceptsRepository,
    ConceptStatesRepository,
    QuestionsRepository,
)
from jolt.data.repositories.credentials import ApiKeyRefRepository
from jolt.data.repositories.reviews import (
    AnswerFeedbackRepository,
    ReviewsRepository,
    SessionsRepository,
)
from jolt.data.repositories.sources import ExtractionsRepository, SourcesRepository
from jolt.data.repositories.tracks import TracksRepository
from jolt.data.repositories.users import UsersRepository


@dataclass
class Repositories:
    users: UsersRepository
    tracks: TracksRepository
    sources: SourcesRepository
    extractions: ExtractionsRepository
    concepts: ConceptsRepository
    concept_states: ConceptStatesRepository
    questions: QuestionsRepository
    reviews: ReviewsRepository
    answer_feedback: AnswerFeedbackRepository
    sessions: SessionsRepository
    api_keys: ApiKeyRefRepository


class Runtime:
    def __init__(
        self,
        settings: Settings,
        cosmos: CosmosGateway,
        blob: BlobGateway,
        vault: VaultClient,
    ) -> None:
        self.settings = settings
        self.cosmos = cosmos
        self.blob = blob
        self.vault = vault
        self.repos = Repositories(
            users=UsersRepository(cosmos),
            tracks=TracksRepository(cosmos),
            sources=SourcesRepository(cosmos),
            extractions=ExtractionsRepository(cosmos),
            concepts=ConceptsRepository(cosmos),
            concept_states=ConceptStatesRepository(cosmos),
            questions=QuestionsRepository(cosmos),
            reviews=ReviewsRepository(cosmos),
            answer_feedback=AnswerFeedbackRepository(cosmos),
            sessions=SessionsRepository(cosmos),
            api_keys=ApiKeyRefRepository(cosmos),
        )

    @classmethod
    async def create(cls, settings: Settings | None = None) -> "Runtime":
        settings = settings or get_settings()
        cosmos = await init_cosmos(settings)
        blob = await init_blob(settings)
        vault = VaultClient(settings)
        return cls(settings, cosmos, blob, vault)

    async def close(self) -> None:
        await self.cosmos.close()
        await self.blob.close()
        await self.vault.close()
