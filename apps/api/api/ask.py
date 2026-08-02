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

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai import build_provider_from_db
from ..core.db import get_db
from ..models.ask_history import AskConversation, AskTurn
from ..models.auth import Admin
from ..models.documents import DocumentSourceType
from ..services.ask_history import AskHistoryError, get_owned_conversation
from ..services.deep_analysis import DeepAnalysisService
from ..services.qa import (
    MAX_QUESTION_LENGTH,
    AskError,
    AskRequest,
    QAService,
)
from .auth import require_admin


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


class ConversationCreatePayload(BaseModel):
    title: str = Field(default="新问答", max_length=255)


class ConversationUpdatePayload(BaseModel):
    title: str = Field(min_length=1, max_length=255)


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _conversation_dict(item: AskConversation, turn_count: int = 0) -> dict[str, Any]:
    return {
        "id": item.id,
        "title": item.title,
        "turn_count": turn_count,
        "last_asked_at": _iso(item.last_asked_at),
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
    }


@router.get("/conversations", response_model=dict[str, Any])
async def list_conversations(
    limit: int = Query(default=30, ge=1, le=100),
    admin: Admin = Depends(require_admin),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    counts = (
        select(AskTurn.conversation_id, func.count(AskTurn.id).label("turn_count"))
        .group_by(AskTurn.conversation_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(AskConversation, func.coalesce(counts.c.turn_count, 0))
            .outerjoin(counts, counts.c.conversation_id == AskConversation.id)
            .where(AskConversation.admin_id == admin.id)
            .order_by(
                AskConversation.last_asked_at.desc().nullslast(),
                AskConversation.created_at.desc(),
            )
            .limit(limit)
        )
    ).all()
    return {"items": [_conversation_dict(item, int(count)) for item, count in rows]}


@router.post("/conversations", response_model=dict[str, Any], status_code=201)
async def create_conversation(
    payload: ConversationCreatePayload,
    admin: Admin = Depends(require_admin),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    title = payload.title.strip() or "新问答"
    item = AskConversation(admin_id=admin.id, title=title)
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return _conversation_dict(item)


@router.get("/conversations/{conversation_id}", response_model=dict[str, Any])
async def get_conversation(
    conversation_id: int,
    admin: Admin = Depends(require_admin),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    try:
        item = await get_owned_conversation(db, conversation_id, admin.id)
    except AskHistoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    turns = list(
        (
            await db.scalars(
                select(AskTurn)
                .where(AskTurn.conversation_id == item.id)
                .order_by(AskTurn.created_at, AskTurn.id)
            )
        ).all()
    )
    return {
        **_conversation_dict(item, len(turns)),
        "turns": [
            {
                "id": turn.id,
                "question": turn.question,
                "mode": turn.mode,
                "context_label": turn.context_label,
                "scope": turn.scope_snapshot,
                "response": turn.response_snapshot,
                "created_at": _iso(turn.created_at),
            }
            for turn in turns
        ],
        "memory_enabled": False,
    }


@router.patch("/conversations/{conversation_id}", response_model=dict[str, Any])
async def update_conversation(
    conversation_id: int,
    payload: ConversationUpdatePayload,
    admin: Admin = Depends(require_admin),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    try:
        item = await get_owned_conversation(db, conversation_id, admin.id)
    except AskHistoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    item.title = payload.title.strip()
    await db.commit()
    await db.refresh(item)
    return _conversation_dict(item)


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: int,
    admin: Admin = Depends(require_admin),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    try:
        item = await get_owned_conversation(db, conversation_id, admin.id)
    except AskHistoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    await db.delete(item)
    await db.commit()
    return Response(status_code=204)


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
