"""Stable read-only knowledge API for CLI, Skills, MCP, and agents."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..ai import build_provider_from_db
from ..core.db import get_db
from ..models.datasets import KnowledgeDataset
from ..models.documents import Document
from ..security.api_auth import APIIdentity, require_api_identity
from ..services.dataset_execution import (
    DatasetExecutionError,
    execute_dataset_query,
    get_dataset_schema,
    preview_dataset,
)
from ..services.deep_analysis import DeepAnalysisService
from ..services.knowledge_read import (
    KnowledgeReadError,
    read_current_chunk,
    read_current_document,
)
from ..services.knowledge_scopes import (
    KnowledgeScopeError,
    KnowledgeScopeResolver,
    list_facet_catalog,
    list_scope_catalog,
)
from ..services.qa import AskError, AskRequest, QAService
from ..services.search import DEFAULT_LIMIT, MAX_LIMIT, search_documents
from .schemas import DatasetFilterInput

router = APIRouter(prefix="/v1", tags=["knowledge-v1"])
logger = logging.getLogger(__name__)

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
    mode: Literal["quick", "deep"] = "quick"


class DatasetQueryPayload(BaseModel):
    filters: list[DatasetFilterInput] = Field(default_factory=list, max_length=8)
    columns: list[str] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list, max_length=3)
    metric: str = "rows"
    metric_column: str | None = None
    sort_by: str | None = None
    sort_order: str = "asc"
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0, le=1_000_000)


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
            "facets": True,
            "lexical_search": True,
            "vector_search": True,
            "cited_qa": True,
            "structured_table_qa": True,
            "deep_analysis": True,
            "dataset_catalog": True,
            "dataset_query": True,
            "dataset_execution_backend": "duckdb_parquet",
            "mcp_ask": True,
            "document_read": True,
            "chunk_read": True,
        },
    }


def _dataset_http_error(exc: DatasetExecutionError) -> HTTPException:
    status = {
        "dataset_not_found": 404,
        "artifact_unavailable": 409,
        "artifact_missing": 409,
        "query_timeout": 408,
    }.get(exc.code, 400)
    return HTTPException(
        status_code=status, detail={"code": exc.code, "message": str(exc)}
    )


@router.get("/knowledge/datasets", response_model=dict[str, Any])
async def list_datasets_v1(
    document_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=200),
    _identity: APIIdentity = Depends(_read_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    statement = (
        select(KnowledgeDataset)
        .join(Document, Document.id == KnowledgeDataset.document_id)
        .where(
            Document.is_deleted.is_(False),
            Document.current_version_id == KnowledgeDataset.document_version_id,
        )
        .order_by(KnowledgeDataset.id.desc())
        .limit(limit)
    )
    if document_id is not None:
        statement = statement.where(KnowledgeDataset.document_id == document_id)
    datasets = list((await db.scalars(statement)).all())
    return {
        "items": [
            {
                "id": item.id,
                "document_id": item.document_id,
                "name": item.name,
                "sheet_name": item.sheet_name,
                "region_index": item.region_index,
                "row_count": item.row_count,
                "column_count": item.column_count,
                "status": item.status,
            }
            for item in datasets
        ],
        "limit": limit,
    }


@router.get("/knowledge/datasets/{dataset_id}/schema", response_model=dict[str, Any])
async def get_dataset_schema_v1(
    dataset_id: int,
    _identity: APIIdentity = Depends(_read_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    try:
        return await get_dataset_schema(db, dataset_id)
    except DatasetExecutionError as exc:
        raise _dataset_http_error(exc) from None


@router.get("/knowledge/datasets/{dataset_id}/rows", response_model=dict[str, Any])
async def preview_dataset_v1(
    dataset_id: int,
    offset: int = Query(default=0, ge=0, le=1_000_000),
    limit: int = Query(default=50, ge=1, le=200),
    _identity: APIIdentity = Depends(_read_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    try:
        return await preview_dataset(db, dataset_id, offset=offset, limit=limit)
    except DatasetExecutionError as exc:
        raise _dataset_http_error(exc) from None


@router.post("/knowledge/datasets/{dataset_id}/query", response_model=dict[str, Any])
async def query_dataset_v1(
    dataset_id: int,
    payload: DatasetQueryPayload,
    _identity: APIIdentity = Depends(_search_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    try:
        return await execute_dataset_query(db, dataset_id, payload.model_dump())
    except DatasetExecutionError as exc:
        raise _dataset_http_error(exc) from None


@router.get("/knowledge/scopes", response_model=dict[str, Any])
async def list_knowledge_scopes(
    _identity: APIIdentity = Depends(_read_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    return {"items": await list_scope_catalog(db)}


@router.get("/knowledge/facets", response_model=dict[str, Any])
async def list_knowledge_facets(
    _identity: APIIdentity = Depends(_read_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    return await list_facet_catalog(db)


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
    service = (
        DeepAnalysisService(provider) if payload.mode == "deep" else QAService(provider)
    )
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


@router.post("/knowledge/ask/stream")
async def knowledge_ask_stream(
    payload: KnowledgeAskPayload,
    _identity: APIIdentity = Depends(_ask_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> StreamingResponse:
    """Stream auditable tool progress and finish with the normal answer DTO."""

    scope = await _resolve_scope(db, payload)
    provider = await build_provider_from_db(db)
    request = AskRequest(
        question=payload.question.strip(),
        category_ids=scope.category_ids,
        tag_ids=scope.tag_ids,
        source_types=scope.source_types,
        document_ids=scope.document_ids,
        connector_ids=scope.connector_ids,
        matches_none=scope.matches_none,
    )
    scope_payload = {
        "id": scope.scope_id,
        "slug": scope.scope_slug,
        **scope.to_filters_dict(),
    }
    if db.bind is None:
        raise HTTPException(status_code=503, detail="数据库连接不可用")
    stream_session_factory = async_sessionmaker(bind=db.bind, expire_on_commit=False)

    async def events() -> AsyncIterator[str]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def on_progress(event: dict[str, Any]) -> None:
            await queue.put({"type": "progress", **event})

        async def run() -> None:
            try:
                async with stream_session_factory() as stream_db:
                    service = (
                        DeepAnalysisService(provider)
                        if payload.mode == "deep"
                        else QAService(provider)
                    )
                    if isinstance(service, DeepAnalysisService):
                        result = await service.ask(
                            stream_db, request, on_progress=on_progress
                        )
                    else:
                        await on_progress(
                            {"phase": "synthesis", "message": "正在检索并生成回答"}
                        )
                        result = await service.ask(stream_db, request)
                await queue.put(
                    {
                        "type": "result",
                        "data": {**result.to_dict(), "scope": scope_payload},
                    }
                )
            except AskError as exc:
                await queue.put(
                    {
                        "type": "error",
                        "error": {"code": exc.code, "message": str(exc)},
                    }
                )
            except Exception:
                logger.exception("streaming knowledge ask failed")
                await queue.put(
                    {
                        "type": "error",
                        "error": {
                            "code": "internal_error",
                            "message": "问答请求失败，请稍后重试",
                        },
                    }
                )

        task = asyncio.create_task(run())
        yield _ndjson(
            {
                "type": "progress",
                "phase": "starting",
                "message": "正在分析问题",
                "tool_calls": 0,
                "max_tool_calls": DeepAnalysisService.default_max_tool_calls,
            }
        )
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=10)
                except TimeoutError:
                    yield _ndjson({"type": "heartbeat"})
                    continue
                yield _ndjson(event)
                if event.get("type") in {"result", "error"}:
                    break
        finally:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


def _ndjson(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str) + "\n"


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
