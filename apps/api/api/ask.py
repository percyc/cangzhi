"""Ask the knowledge base a question and get a cited answer.

The endpoint is intentionally thin:

* It validates the request and forwards it to :class:`QAService`.
* The service owns retrieval, model dispatch and citation checking.
* Errors are mapped to small, stable response codes so the front end
  can render specific empty / unconfigured / provider-failed states
  without parsing free-form messages.

The first cut only supports the FTS-based retrieval flow. Vector
recall, RRF fusion, reranking and conversational memory are tracked
in ``docs/ROADMAP.md`` M3 follow-ups.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai import build_provider_from_db
from ..core.db import get_db
from ..models.documents import DocumentSourceType
from ..services.deep_analysis import DeepAnalysisService
from ..services.qa import (
    MAX_QUESTION_LENGTH,
    AskError,
    AskRequest,
    QAService,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ask", tags=["ask"])


_ASK_ERROR_STATUS = {
    "empty_question": 400,
    "question_too_long": 400,
    "invalid_filters": 400,
    "provider_not_configured": 503,
    "provider_failed": 502,
}


class AskPayload(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)
    mode: Literal["quick", "deep"] = "quick"
    category_ids: list[int] = Field(default_factory=list)
    category_slugs: list[str] = Field(default_factory=list)
    tag_ids: list[int] = Field(default_factory=list)
    tag_slugs: list[str] = Field(default_factory=list)
    source_types: list[str] = Field(default_factory=list)
    connector_ids: list[int] = Field(default_factory=list)
    document_ids: list[int] = Field(default_factory=list)

    @field_validator("source_types")
    @classmethod
    def _validate_source_types(cls, value: list[str]) -> list[str]:
        allowed = {item.value for item in DocumentSourceType}
        cleaned: list[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            candidate = item.strip().lower()
            if candidate in allowed and candidate not in cleaned:
                cleaned.append(candidate)
        return cleaned

    @field_validator("category_slugs", "tag_slugs")
    @classmethod
    def _normalize_slugs(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        cleaned: list[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            slug = item.strip().lower()
            if slug and slug not in seen:
                seen.add(slug)
                cleaned.append(slug)
        return cleaned

    def to_request(self) -> AskRequest:
        return AskRequest(
            question=self.question.strip(),
            category_ids=list(self.category_ids),
            category_slugs=list(self.category_slugs),
            tag_ids=list(self.tag_ids),
            tag_slugs=list(self.tag_slugs),
            source_types=list(self.source_types),
            connector_ids=list(self.connector_ids),
            document_ids=list(self.document_ids),
        )


@router.post("", response_model=dict[str, Any])
async def ask_post(
    payload: AskPayload,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    provider = await build_provider_from_db(db)
    service = (
        DeepAnalysisService(provider)
        if payload.mode == "deep"
        else QAService(provider)
    )
    if not service.is_provider_configured:
        # The retrieval layer does not need the model, but asking
        # without a configured model would just return
        # ``insufficient_evidence`` and look like the model rejected
        # the question. Telling the user up front is more honest.
        raise HTTPException(
            status_code=503,
            detail={
                "code": "provider_not_configured",
                "message": "尚未配置问答模型，请先完成模型设置",
            },
        )
    try:
        result = await service.ask(db, payload.to_request())
    except AskError as exc:
        status = _ASK_ERROR_STATUS.get(exc.code, 500)
        logger.warning("ask failed code=%s status=%s", exc.code, status)
        raise HTTPException(
            status_code=status,
            detail={"code": exc.code, "message": str(exc)},
        ) from None
    return result.to_dict()


@router.get("/status", response_model=dict[str, Any])
async def ask_status(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Return whether the Q&A feature is currently usable.

    The front end uses this to render a friendly placeholder on
    ``/ask`` before the user has typed anything.
    """

    provider = await build_provider_from_db(db)
    configured = provider is not None and provider.is_configured()
    return {
        "provider_configured": configured,
        "provider": getattr(provider, "name", "") if provider else "",
    }
