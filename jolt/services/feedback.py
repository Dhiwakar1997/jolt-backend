"""Understanding feedback on the user's own key (BE-11).

`POST /v1/concepts/{id}/feedback` explains how the learner's understanding of
one concept has moved, from the concept text and their answer timeline only,
and suggests one concrete next step. With a `review_id` it explains that one
answer instead (the result screen's "Explain").

Runs only while the app is open: one synchronous request, no background job.
The answer is cached on the learner's card until the timeline changes (a new
answer, or a grade landing from the nightly sync) or the concept text changes;
a cached answer costs no model call. Order of checks: ownership (404), a
stored key (`key_required`, before anything else that could reach a
provider), an answer to talk about (`no_answers`), the cache, the spend guard,
then the call.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from jolt.credentials.inference import call_model, resolve_provider
from jolt.credentials.prompts import (
    EXPLAIN_SYSTEM,
    FEEDBACK_PROMPT_VERSION,
    FEEDBACK_SYSTEM,
    TimelineAnswer,
    feedback_message,
)
from jolt.credentials.providers import (
    PROVIDER_ERROR,
    ModelCallError,
    ModelRequest,
    TextPart,
    parse_json_answer,
)
from jolt.domain.models import ConceptState, FeedbackCache
from jolt.runtime import Runtime
from jolt.services.ownership import NotOwned, owned_concept
from jolt.services.read_models import ReadModelService
from jolt.services.reviews import ReviewService
from jolt.services.spend import SpendGuard, UsageView

NO_ANSWERS = "no_answers"
FEEDBACK_MAX_CHARS = 2000


@dataclass
class FeedbackResult:
    concept_id: str
    review_id: Optional[str]
    feedback: str
    next_step: str
    provider: str
    model_id: Optional[str]
    answers: int
    generated_at: datetime
    cached: bool
    usage: UsageView
    input_tokens: int = 0
    output_tokens: int = 0


def fingerprint(concept_text: str, answers: list[TimelineAnswer], review_id: Optional[str]) -> str:
    """Changes whenever the feedback would: a new answer, a grade or grader
    feedback landing, a corrected concept text, a new prompt version."""
    payload = json.dumps(
        [
            FEEDBACK_PROMPT_VERSION,
            concept_text,
            review_id,
            [[a.review_id, a.grade, a.grading, a.grader_feedback, a.free_text] for a in answers],
        ],
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


class FeedbackService:
    def __init__(self, rt: Runtime) -> None:
        self._rt = rt

    async def _answers(self, user_id: str, concept_id: str) -> list[TimelineAnswer]:
        history = await self._rt.repos.reviews.history_for_concept(user_id, concept_id)
        questions = {
            q.id: q
            for q in await self._rt.repos.questions.list_for_concept(concept_id, active_only=False)
        }
        out: list[TimelineAnswer] = []
        for entry in ReadModelService._timeline(history):
            q = questions.get(entry.question_id)
            out.append(
                TimelineAnswer(
                    review_id=entry.review_id,
                    reviewed_at=entry.reviewed_at,
                    free_text=entry.free_text,
                    is_correct=entry.is_correct,
                    grade=entry.grade,
                    grading=entry.grading,
                    grader_feedback=entry.feedback,
                    question_stem=q.stem if q else None,
                    question_version=entry.question_version,
                    expected_answer=q.expected_answer if q else None,
                )
            )
        return out

    async def feedback(
        self,
        user_id: str,
        concept_id: str,
        *,
        review_id: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> FeedbackResult:
        concept, state = await owned_concept(self._rt, user_id, concept_id)
        chosen = await resolve_provider(self._rt, user_id, provider)
        answers = await self._answers(user_id, concept_id)
        if review_id is not None and all(a.review_id != review_id for a in answers):
            raise NotOwned("review not found")
        if not answers:
            raise ModelCallError(
                NO_ANSWERS,
                "answer this concept at least once; feedback reads your answers",
                status=409,
            )

        slot = "explanation" if review_id else "feedback"
        print_ = fingerprint(concept.text, answers, review_id)
        cached: Optional[FeedbackCache] = getattr(state, slot)
        if cached is not None and cached.fingerprint == print_:
            return FeedbackResult(
                concept_id=concept_id,
                review_id=review_id,
                feedback=cached.feedback,
                next_step=cached.next_step,
                provider=cached.provider,
                model_id=cached.model_id,
                answers=cached.answers,
                generated_at=cached.generated_at,
                cached=True,
                usage=await SpendGuard(self._rt).today(user_id),
            )

        title = ReviewService.concept_title(concept, concept.id)
        request = ModelRequest(
            system=EXPLAIN_SYSTEM if review_id else FEEDBACK_SYSTEM,
            parts=[
                TextPart(
                    feedback_message(
                        title=title,
                        concept_text=concept.text,
                        answers=answers,
                        focus_review_id=review_id,
                    )
                )
            ],
            max_tokens=self._rt.settings.model_feedback_max_tokens,
            effort="low",
            timeout=self._rt.settings.model_feedback_timeout_seconds,
        )
        call = await call_model(self._rt, user_id, chosen, "feedback", request)
        answer = parse_json_answer(call.response.text) or {}
        text = str(answer.get("feedback") or "").strip()[:FEEDBACK_MAX_CHARS]
        next_step = str(answer.get("next_step") or "").strip()[:FEEDBACK_MAX_CHARS]
        if not text or not next_step:
            raise ModelCallError(PROVIDER_ERROR, "the model's answer could not be read; try again")

        entry = FeedbackCache(
            fingerprint=print_,
            review_id=review_id,
            feedback=text,
            next_step=next_step,
            provider=call.provider,
            model_id=call.response.model_id,
            answers=len(answers),
            generated_at=datetime.now(timezone.utc),
        )
        await self._store(user_id, concept_id, slot, entry)
        return FeedbackResult(
            concept_id=concept_id,
            review_id=review_id,
            feedback=text,
            next_step=next_step,
            provider=call.provider,
            model_id=call.response.model_id,
            answers=len(answers),
            generated_at=entry.generated_at,
            cached=False,
            usage=call.usage,
            input_tokens=call.response.input_tokens,
            output_tokens=call.response.output_tokens,
        )

    async def _store(self, user_id: str, concept_id: str, slot: str, entry: FeedbackCache) -> None:
        """Etag-guarded write on the card; best effort (a lost race only means
        the next request asks the model again)."""
        for _ in range(3):
            state: Optional[ConceptState] = await self._rt.repos.concept_states.for_concept(
                user_id, concept_id
            )
            if state is None:
                return
            setattr(state, slot, entry)
            if await self._rt.repos.concept_states.replace(state, etag=state.etag) is not None:
                return
