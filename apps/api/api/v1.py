"""Stable read-only knowledge API for CLI, Skills, MCP, and agents."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai import build_provider_from_db
from ..core.db import get_db
from ..security.api_auth import APIIdentity, require_api_identity
from ..services.knowledge_read import (
    KnowledgeReadError,
    read_current_chunk,
    read_current_document,
)
from ..services.knowledge_scopes import (
    KnowledgeScopeError,
    KnowledgeScopeResolver,
    list_scope_catalog,
)
from ..services.qa import AskError, AskRequest, QAService
from ..services.search import DEFAULT_LIMIT, MAX_LIMIT, search_documents

router = APIRouter(prefix="/v1", tags=["knowledge-v1"])

_read_identity = require_api_identity("knowledge:read")
_search_identity = require_api_identity("knowledge:search")
_ask_identity = require_api_identity("knowledge:ask")


class ScopeSelector(BaseModel):
    scope_id: int | None = Field(default=None, ge=1)
    scope_slug: str | None = Field(default=None, max_length=128)
    category_ids: list[int] = Field(default_factory=list)
    tag_ids: list[int] = Field(default_factory=list)
    source_types: list[str] = Field(default_factory=list)
    connector_ids: list[int] = Field(default_factory=list)
    document_ids: list[int] = Field(default_factory=list)


class KnowledgeSearchPayload(ScopeSelector):
    query: str = Field(min_length=1, max_length=512)
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    offset: int = Field(default=0, ge=0, le=10_000)


class KnowledgeAskPayload(ScopeSelector):
    question: str = Field(min_length=1, max_length=500)


def _scope_error(exc: KnowledgeScopeError) -> HTTPException:
    status = 404 if exc.code == "scope_not_found" else 400
    return HTTPException(
        status_code=status,
        detail={"code": exc.code, "message": str(exc)},
    )


async def _resolve_scope(db: AsyncSession, payload: ScopeSelector):
    try:
        return await KnowledgeScopeResolver(db).resolve(
            scope_id=payload.scope_id,
            scope_slug=payload.scope_slug,
            category_ids=payload.category_ids,
            tag_ids=payload.tag_ids,
            source_types=payload.source_types,
            connector_ids=payload.connector_ids,
            document_ids=payload.document_ids,
        )
    except KnowledgeScopeError as exc:
        raise _scope_error(exc) from None


@router.get("/capabilities", response_model=dict[str, Any])
async def capabilities(
    _identity: APIIdentity = Depends(_read_identity),  # noqa: B008
) -> dict[str, Any]:
    return {
        "api_version": "v1",
        "service": "cangzhi",
        "authentication": ["bearer_pat", "session_cookie"],
        "scopes": ["knowledge:read", "knowledge:search", "knowledge:ask"],
        "features": {
            "saved_scopes": True,
            "lexical_search": True,
            "vector_search": True,
            "cited_qa": True,
            "document_read": True,
            "chunk_read": True,
        },
    }


@router.get("/knowledge/scopes", response_model=dict[str, Any])
async def list_knowledge_scopes(
    _identity: APIIdentity = Depends(_read_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    return {"items": await list_scope_catalog(db)}


@router.post("/knowledge/search", response_model=dict[str, Any])
async def knowledge_search(
    payload: KnowledgeSearchPayload,
    _identity: APIIdentity = Depends(_search_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    scope = await _resolve_scope(db, payload)
    result = await search_documents(
        db,
        query=payload.query.strip(),
        limit=payload.limit,
        offset=payload.offset,
        category_ids=scope.category_ids,
        tag_ids=scope.tag_ids,
        source_types=scope.source_types,
        document_ids=scope.document_ids,
        connector_ids=scope.connector_ids,
        matches_none=scope.matches_none,
    )
    return {
        **result.to_dict(),
        "scope": {
            "id": scope.scope_id,
            "slug": scope.scope_slug,
            **scope.to_filters_dict(),
        },
    }


@router.post("/knowledge/ask", response_model=dict[str, Any])
async def knowledge_ask(
    payload: KnowledgeAskPayload,
    _identity: APIIdentity = Depends(_ask_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    scope = await _resolve_scope(db, payload)
    provider = await build_provider_from_db(db)
    service = QAService(provider)
    try:
        result = await service.ask(
            db,
            AskRequest(
                question=payload.question.strip(),
                category_ids=scope.category_ids,
                tag_ids=scope.tag_ids,
                source_types=scope.source_types,
                document_ids=scope.document_ids,
                connector_ids=scope.connector_ids,
                matches_none=scope.matches_none,
            ),
        )
    except AskError as exc:
        status = {
            "empty_question": 400,
            "question_too_long": 400,
            "provider_not_configured": 503,
            "provider_failed": 502,
        }.get(exc.code, 500)
        raise HTTPException(
            status_code=status,
            detail={"code": exc.code, "message": str(exc)},
        ) from None
    return {
        **result.to_dict(),
        "scope": {
            "id": scope.scope_id,
            "slug": scope.scope_slug,
            **scope.to_filters_dict(),
        },
    }


@router.get("/knowledge/documents/{document_id}", response_model=dict[str, Any])
async def get_knowledge_document(
    document_id: int,
    _identity: APIIdentity = Depends(_read_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    try:
        return await read_current_document(db, document_id)
    except KnowledgeReadError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": exc.code, "message": str(exc)},
        ) from None


@router.get("/knowledge/chunks/{chunk_id}", response_model=dict[str, Any])
async def get_knowledge_chunk(
    chunk_id: int,
    _identity: APIIdentity = Depends(_read_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    try:
        return await read_current_chunk(db, chunk_id)
    except KnowledgeReadError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": exc.code, "message": str(exc)},
        ) from None
