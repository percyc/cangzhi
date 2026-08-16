"""Stateless MCP Streamable HTTP adapter for read-only knowledge tools."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai import build_provider_from_db
from ..core.db import get_db
from ..models.chunks import DocumentChunk
from ..models.datasets import KnowledgeDataset
from ..security.api_auth import APIIdentity, require_api_identity
from ..services.access_keys import ensure_document_visible
from ..services.dataset_execution import (
    DatasetExecutionError,
    execute_dataset_query,
    get_dataset_schema,
    list_visible_datasets,
    preview_dataset,
)
from ..services.evidence import EvidenceError, EvidenceService
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
from ..services.search import search_documents
from .schemas import DatasetFilterInput

router = APIRouter(prefix="/mcp", tags=["mcp"])
logger = logging.getLogger(__name__)
_mcp_identity = require_api_identity()
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")

ProgressCallback = Callable[[int, int, str], Awaitable[None]]


class MCPRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: str | int | None = None
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class SearchArguments(BaseModel):
    query: str = Field(min_length=1, max_length=512)
    limit: int = Field(default=10, ge=1, le=50)
    offset: int = Field(default=0, ge=0, le=10_000)
    scope_id: int | None = Field(default=None, ge=1)
    scope_slug: str | None = Field(default=None, max_length=128)
    category_ids: list[int] = Field(default_factory=list)
    tag_ids: list[int] = Field(default_factory=list)
    source_types: list[str] = Field(default_factory=list)
    connector_ids: list[int] = Field(default_factory=list)
    document_ids: list[int] = Field(default_factory=list)
    access_key: str | None = Field(default=None, max_length=128)


class AskArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=500)
    scope_id: int | None = Field(default=None, ge=1)
    scope_slug: str | None = Field(default=None, max_length=128)
    category_ids: list[int] = Field(default_factory=list)
    tag_ids: list[int] = Field(default_factory=list)
    source_types: list[str] = Field(default_factory=list)
    connector_ids: list[int] = Field(default_factory=list)
    document_ids: list[int] = Field(default_factory=list)
    access_key: str | None = Field(default=None, max_length=128)


class DocumentReadArguments(BaseModel):
    document_id: int = Field(ge=1)
    offset: int = Field(default=0, ge=0)
    max_chars: int = Field(default=12_000, ge=1, le=50_000)
    access_key: str | None = Field(default=None, max_length=128)


class ChunkReadArguments(BaseModel):
    chunk_id: int = Field(ge=1)
    access_key: str | None = Field(default=None, max_length=128)


class DatasetListArguments(BaseModel):
    document_id: int | None = Field(default=None, ge=1)
    limit: int = Field(default=100, ge=1, le=200)
    access_key: str | None = Field(default=None, max_length=128)


class DatasetIdArguments(BaseModel):
    dataset_id: int = Field(ge=1)
    access_key: str | None = Field(default=None, max_length=128)


class DatasetPreviewArguments(DatasetIdArguments):
    offset: int = Field(default=0, ge=0, le=1_000_000)
    limit: int = Field(default=50, ge=1, le=200)


class DatasetQueryArguments(DatasetIdArguments):
    filters: list[DatasetFilterInput] = Field(default_factory=list, max_length=8)
    columns: list[str] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list, max_length=3)
    metric: str = "rows"
    metric_column: str | None = None
    sort_by: str | None = None
    sort_order: str = "asc"
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0, le=1_000_000)


class EvidenceChunkArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: int = Field(ge=1)
    document_version_id: int = Field(ge=1)
    access_key: str | None = Field(default=None, max_length=128)


class EvidenceDatasetArguments(DatasetIdArguments):
    model_config = ConfigDict(extra="forbid")

    document_version_id: int = Field(ge=1)
    artifact_version: int | None = Field(default=None, ge=1)


class EvidenceRowsArguments(DatasetIdArguments):
    model_config = ConfigDict(extra="forbid")

    document_version_id: int = Field(ge=1)
    artifact_version: int | None = Field(default=None, ge=1)
    source_rows: list[int] = Field(default_factory=list, max_length=200)
    columns: list[str] = Field(default_factory=list, max_length=64)
    limit: int = Field(default=20, ge=1, le=200)


TOOLS = [
    {
        "name": "knowledge_list_scopes",
        "title": "列出知识范围",
        "description": "列出全部、随手记、网页、文件和用户保存的知识范围。",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "knowledge_list_facets",
        "title": "列出知识筛选项",
        "description": "列出可用于收窄检索的分类、标签、来源类型和 WebDAV 连接器。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "access_key": {"type": "string", "minLength": 1, "maxLength": 128}
            },
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "knowledge_search",
        "title": "检索藏知",
        "description": (
            "从藏知检索相关证据片段；适合由外部模型自行组织回答。hits[].context "
            "是包含相邻片段的完整证据窗口，snippet 仅用于命中预览；"
            "retrieval_channels 同时包含 lexical 和 vector 时表示两个检索通道共同支持。"
            "access_key 仅是受信任调用方提供的检索范围，不是独立身份认证。"
        ),
        "inputSchema": SearchArguments.model_json_schema(),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "knowledge_ask",
        "title": "询问藏知",
        "description": (
            "由藏知执行一次快速检索并回答问题，返回可核验引用。表格的筛选、"
            "明细、统计、分组和排序会优先使用精确计算。外部 Agent 的复杂分析"
            "应自行组合搜索、读取和数据集工具；本工具不提供 deep 模式。"
        ),
        "inputSchema": AskArguments.model_json_schema(),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "knowledge_get_document",
        "title": "读取知识文档",
        "description": (
            "按文档 ID 分页读取当前正文，默认最多 12000 字符，并返回总长度、"
            "截断状态和 next_offset。大型 Excel 不应整篇读取：精确筛选或统计"
            "请使用 knowledge_ask，定位证据请使用 knowledge_search 和 "
            "knowledge_get_chunk。"
        ),
        "inputSchema": DocumentReadArguments.model_json_schema(),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "knowledge_get_chunk",
        "title": "读取知识片段",
        "description": "按片段 ID 读取当前有效片段及可定位元数据。",
        "inputSchema": ChunkReadArguments.model_json_schema(),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "knowledge_list_datasets",
        "title": "列出结构化数据集",
        "description": "列出当前有效文档中的二维数据集。先发现数据集，再读取字段或执行查询。",
        "inputSchema": DatasetListArguments.model_json_schema(),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "knowledge_get_dataset_schema",
        "title": "读取数据集结构",
        "description": "读取字段类型、语义角色、样例、统计画像和执行后端；规划查询前应先调用。",
        "inputSchema": DatasetIdArguments.model_json_schema(),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "knowledge_preview_dataset_rows",
        "title": "预览数据集行",
        "description": "分页预览少量行。禁止用它逐页抓取整个大表；筛选、统计和分组请使用 knowledge_query_dataset。",
        "inputSchema": DatasetPreviewArguments.model_json_schema(),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "knowledge_query_dataset",
        "title": "精确查询数据集",
        "description": (
            "通过受控计划执行投影、筛选、排序、分组和聚合，由 DuckDB/Parquet 下推计算。"
            "不接受 SQL，最多返回 200 行，适合大型 Excel，避免把整表放入模型上下文。"
            "filters 每项使用 column、operator、value；operator 支持 "
            "eq/ne/gt/gte/lt/lte/contains/starts_with/ends_with/"
            "direct_child_of/in。direct_child_of 用于安全选择层级路径的直属子级。"
            "零结果时应检查响应中的 query_hints，并按 suggested_filter 调整后继续查询；"
            "不要因为首次精确查询为空就结束探索。"
        ),
        "inputSchema": DatasetQueryArguments.model_json_schema(),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "knowledge_get_evidence_by_chunk",
        "title": "按片段读取证据上下文",
        "description": (
            "读取版本绑定的证据抽屉上下文：Markdown 节选/章节高亮、"
            "PDF/Word 解析段加 PDF 页码跳转、数据集精确计算结果与贡献行。"
            "必须传入与原始回答相同的 document_version_id；版本不匹配时返回错误，"
            "避免读取新版本冒充原始证据。"
        ),
        "inputSchema": EvidenceChunkArguments.model_json_schema(),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "knowledge_get_evidence_by_dataset",
        "title": "按数据集读取证据上下文",
        "description": (
            "读取与回答绑定版本的数据集证据上下文：精确计划、贡献行、"
            "统计结果、列式产物版本号。同样要求 document_version_id 与回答一致。"
        ),
        "inputSchema": EvidenceDatasetArguments.model_json_schema(),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "knowledge_preview_evidence_rows",
        "title": "按 source_rows 预览贡献行",
        "description": (
            "仅返回 evidence.source_rows 列表中明确请求的原始行；不返回 SQL，"
            "不放任把整张表装载入上下文。受限 200 行以内，分页由调用方控制。"
        ),
        "inputSchema": EvidenceRowsArguments.model_json_schema(),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
]


def _result(request_id: str | int | None, value: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def _error(
    request_id: str | int | None,
    code: int,
    message: str,
    *,
    data: dict | None = None,
) -> dict:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _tool_result(payload: dict) -> dict:
    text = json.dumps(payload, ensure_ascii=False)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload,
        "isError": False,
    }


def _tool_error(code: str, message: str) -> dict:
    payload = {"error": {"code": code, "message": message}}
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "structuredContent": payload,
        "isError": True,
    }


def _sse_event(message: dict) -> str:
    return f"data: {json.dumps(message, ensure_ascii=False)}\n\n"


async def _stream_ask_result(
    request_id: str | int | None,
    name: str,
    arguments: dict[str, Any],
    identity: APIIdentity,
    db: AsyncSession,
    progress_token: str | int,
):
    """Stream ``tools/call`` progress notifications then the final result.

    Mirrors the MCP 2025-06-18 Streamable HTTP shape: any number of
    ``notifications/progress`` events are emitted first, and the response
    closes with the JSON-RPC result carrying the original request id.
    """
    queue: asyncio.Queue[tuple[int, int, str] | None] = asyncio.Queue()
    last_progress = -1

    async def report(progress: int, total: int, message: str) -> None:
        nonlocal last_progress
        if progress <= last_progress:
            return
        last_progress = progress
        await queue.put((progress, total, message))

    async def run() -> dict:
        try:
            payload = await _call_tool(
                name, arguments, identity, db, on_progress=report
            )
            if not payload.get("isError"):
                await report(100, 100, "回答完成")
            return payload
        finally:
            await queue.put(None)

    task = asyncio.create_task(run())
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            progress, total, message = item
            yield _sse_event(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/progress",
                    "params": {
                        "progressToken": progress_token,
                        "progress": progress,
                        "total": total,
                        "message": message,
                    },
                }
            )
        try:
            payload = task.result()
        except Exception:
            logger.warning("knowledge_ask stream aborted", exc_info=True)
            yield _sse_event(_error(request_id, -32603, "Internal error"))
            return
        yield _sse_event(_result(request_id, payload))
    finally:
        if not task.done():
            task.cancel()
        # Always consume the task result so a client disconnect cannot leave
        # an unobserved exception behind in the event loop.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def _require_scope(identity: APIIdentity, scope: str) -> dict | None:
    if identity.has_scope(scope):
        return None
    return _tool_error("insufficient_scope", f"令牌缺少权限：{scope}")


def _window_document(payload: dict[str, Any], args: DocumentReadArguments) -> dict:
    structured = payload.get("structured_content")
    content = payload.get("content")
    if not isinstance(content, str) and isinstance(structured, dict):
        blocks = structured.get("blocks")
        if isinstance(blocks, list):
            content = "\n\n".join(
                str(block.get("text") or "")
                for block in blocks
                if isinstance(block, dict) and block.get("text")
            )
    content = content if isinstance(content, str) else ""
    total_chars = len(content)
    start = min(args.offset, total_chars)
    end = min(start + args.max_chars, total_chars)
    metadata = structured.get("metadata") if isinstance(structured, dict) else {}
    safe_metadata_keys = {
        "parser",
        "filename",
        "sheet_count",
        "row_count",
        "column_count",
        "data_region_count",
        "spreadsheet_schema_version",
    }
    safe_metadata = (
        {
            key: metadata[key]
            for key in safe_metadata_keys
            if isinstance(metadata, dict) and key in metadata
        }
        if isinstance(metadata, dict)
        else {}
    )
    result = {
        key: value for key, value in payload.items() if key != "structured_content"
    }
    result["content"] = content[start:end]
    result["structure"] = {
        "document_type": (
            structured.get("document_type") if isinstance(structured, dict) else None
        ),
        "block_count": (
            len(structured.get("blocks") or [])
            if isinstance(structured, dict)
            and isinstance(structured.get("blocks"), list)
            else 0
        ),
        "metadata": safe_metadata,
    }
    result["content_window"] = {
        "offset": start,
        "returned_chars": end - start,
        "total_chars": total_chars,
        "truncated": end < total_chars,
        "next_offset": end if end < total_chars else None,
        "max_chars": args.max_chars,
    }
    return result


async def _call_tool(
    name: str,
    arguments: dict[str, Any],
    identity: APIIdentity,
    db: AsyncSession,
    on_progress: ProgressCallback | None = None,
) -> dict:
    if name == "knowledge_search":
        forbidden = _require_scope(identity, "knowledge:search")
        if forbidden:
            return forbidden
        try:
            args = SearchArguments.model_validate(arguments)
            scope = await KnowledgeScopeResolver(db).resolve(
                scope_id=args.scope_id,
                scope_slug=args.scope_slug,
                category_ids=args.category_ids,
                tag_ids=args.tag_ids,
                source_types=args.source_types,
                connector_ids=args.connector_ids,
                document_ids=args.document_ids,
            )
            result = await search_documents(
                db,
                query=args.query,
                limit=args.limit,
                offset=args.offset,
                category_ids=scope.category_ids,
                tag_ids=scope.tag_ids,
                source_types=scope.source_types,
                connector_ids=scope.connector_ids,
                document_ids=scope.document_ids,
                access_key=args.access_key,
                matches_none=scope.matches_none,
            )
        except ValidationError as exc:
            return _tool_error("invalid_arguments", str(exc))
        except KnowledgeScopeError as exc:
            return _tool_error(exc.code, str(exc))
        return _tool_result(
            {
                **result.to_dict(),
                "scope": {
                    "id": scope.scope_id,
                    "slug": scope.scope_slug,
                    "access_key": args.access_key,
                    **scope.to_filters_dict(),
                },
            }
        )

    if name == "knowledge_ask":
        forbidden = _require_scope(identity, "knowledge:ask")
        if forbidden:
            return forbidden
        try:
            args = AskArguments.model_validate(arguments)
            scope = await KnowledgeScopeResolver(db).resolve(
                scope_id=args.scope_id,
                scope_slug=args.scope_slug,
                category_ids=args.category_ids,
                tag_ids=args.tag_ids,
                source_types=args.source_types,
                connector_ids=args.connector_ids,
                document_ids=args.document_ids,
            )
            provider = await build_provider_from_db(db)
            result = await QAService(provider).ask(
                db,
                AskRequest(
                    question=args.question.strip(),
                    category_ids=scope.category_ids,
                    tag_ids=scope.tag_ids,
                    source_types=scope.source_types,
                    connector_ids=scope.connector_ids,
                    document_ids=scope.document_ids,
                    access_key=args.access_key,
                    matches_none=scope.matches_none,
                ),
                on_progress=on_progress,
            )
        except ValidationError as exc:
            return _tool_error("invalid_arguments", str(exc))
        except KnowledgeScopeError as exc:
            return _tool_error(exc.code, str(exc))
        except AskError as exc:
            return _tool_error(exc.code, str(exc))
        return _tool_result(
            {
                **result.to_dict(),
                "scope": {
                    "id": scope.scope_id,
                    "slug": scope.scope_slug,
                    "access_key": args.access_key,
                    **scope.to_filters_dict(),
                },
            }
        )

    if name == "knowledge_query_dataset":
        forbidden = _require_scope(identity, "knowledge:search")
        if forbidden:
            return forbidden
        try:
            args = DatasetQueryArguments.model_validate(arguments)
            document_id = await db.scalar(select(KnowledgeDataset.document_id).where(KnowledgeDataset.id == args.dataset_id))
            if document_id is not None:
                await ensure_document_visible(db, document_id, args.access_key)
            payload = args.model_dump(exclude={"dataset_id", "access_key"})
            return _tool_result(
                await execute_dataset_query(db, args.dataset_id, payload)
            )
        except ValidationError as exc:
            return _tool_error("invalid_arguments", str(exc))
        except DatasetExecutionError as exc:
            return _tool_error(exc.code, str(exc))

    if name in {
        "knowledge_get_evidence_by_chunk",
        "knowledge_get_evidence_by_dataset",
        "knowledge_preview_evidence_rows",
    }:
        forbidden = _require_scope(identity, "knowledge:read")
        if forbidden:
            return forbidden
        try:
            service = EvidenceService()
            if name == "knowledge_get_evidence_by_chunk":
                args = EvidenceChunkArguments.model_validate(arguments)
                document_id = await db.scalar(select(DocumentChunk.document_id).where(DocumentChunk.id == args.chunk_id))
                if document_id is not None:
                    await ensure_document_visible(db, document_id, args.access_key)
                return _tool_result(
                    (
                        await service.resolve_chunk(
                            db,
                            chunk_id=args.chunk_id,
                            document_version_id=args.document_version_id,
                        )
                    ).to_dict()
                )
            if name == "knowledge_get_evidence_by_dataset":
                args = EvidenceDatasetArguments.model_validate(arguments)
                document_id = await db.scalar(select(KnowledgeDataset.document_id).where(KnowledgeDataset.id == args.dataset_id))
                if document_id is not None:
                    await ensure_document_visible(db, document_id, args.access_key)
                return _tool_result(
                    (
                        await service.resolve_dataset(
                            db,
                            dataset_id=args.dataset_id,
                            document_version_id=args.document_version_id,
                            artifact_version=args.artifact_version,
                        )
                    ).to_dict()
                )
            args = EvidenceRowsArguments.model_validate(arguments)
            document_id = await db.scalar(select(KnowledgeDataset.document_id).where(KnowledgeDataset.id == args.dataset_id))
            if document_id is not None:
                await ensure_document_visible(db, document_id, args.access_key)
            return _tool_result(
                await service.preview_dataset_rows(
                    db,
                    dataset_id=args.dataset_id,
                    document_version_id=args.document_version_id,
                    artifact_version=args.artifact_version,
                    source_rows=args.source_rows,
                    columns=args.columns,
                    limit=args.limit,
                )
            )
        except ValidationError as exc:
            return _tool_error("invalid_arguments", str(exc))
        except EvidenceError as exc:
            return _tool_error(exc.code, str(exc))

    forbidden = _require_scope(identity, "knowledge:read")
    if forbidden:
        return forbidden
    try:
        if name == "knowledge_list_scopes":
            return _tool_result({"items": await list_scope_catalog(db)})
        if name == "knowledge_list_facets":
            access_key = str(arguments.get("access_key") or "").strip() or None
            return _tool_result(await list_facet_catalog(db, access_key=access_key))
        if name == "knowledge_get_document":
            args = DocumentReadArguments.model_validate(arguments)
            await ensure_document_visible(db, args.document_id, args.access_key)
            document = await read_current_document(db, args.document_id)
            return _tool_result(_window_document(document, args))
        if name == "knowledge_get_chunk":
            args = ChunkReadArguments.model_validate(arguments)
            document_id = await db.scalar(select(DocumentChunk.document_id).where(DocumentChunk.id == args.chunk_id))
            if document_id is not None:
                await ensure_document_visible(db, document_id, args.access_key)
            return _tool_result(await read_current_chunk(db, args.chunk_id))
        if name == "knowledge_list_datasets":
            args = DatasetListArguments.model_validate(arguments)
            return _tool_result(
                {
                    "items": await list_visible_datasets(
                        db, document_id=args.document_id, limit=args.limit
                        , access_key=args.access_key
                    )
                }
            )
        if name == "knowledge_get_dataset_schema":
            args = DatasetIdArguments.model_validate(arguments)
            document_id = await db.scalar(select(KnowledgeDataset.document_id).where(KnowledgeDataset.id == args.dataset_id))
            if document_id is not None:
                await ensure_document_visible(db, document_id, args.access_key)
            return _tool_result(await get_dataset_schema(db, args.dataset_id))
        if name == "knowledge_preview_dataset_rows":
            args = DatasetPreviewArguments.model_validate(arguments)
            document_id = await db.scalar(select(KnowledgeDataset.document_id).where(KnowledgeDataset.id == args.dataset_id))
            if document_id is not None:
                await ensure_document_visible(db, document_id, args.access_key)
            return _tool_result(
                await preview_dataset(
                    db, args.dataset_id, offset=args.offset, limit=args.limit
                )
            )
    except (TypeError, ValueError, ValidationError):
        return _tool_error(
            "invalid_arguments",
            "读取参数无效，请检查 ID、offset 和 max_chars",
        )
    except KnowledgeReadError as exc:
        return _tool_error(exc.code, str(exc))
    except DatasetExecutionError as exc:
        return _tool_error(exc.code, str(exc))
    return _tool_error("tool_not_found", f"未知工具：{name}")


@router.post("")
async def mcp_post(
    payload: MCPRequest,
    request: Request,
    identity: APIIdentity = Depends(_mcp_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
):
    if payload.jsonrpc != "2.0":
        return _error(payload.id, -32600, "Invalid Request")
    if payload.method == "notifications/initialized":
        return Response(status_code=202)
    if payload.method == "initialize":
        requested = payload.params.get("protocolVersion")
        protocol = (
            requested
            if requested in SUPPORTED_PROTOCOL_VERSIONS
            else SUPPORTED_PROTOCOL_VERSIONS[0]
        )
        return _result(
            payload.id,
            {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "cangzhi", "version": "0.3.0"},
                "instructions": (
                    "范围不明确时先使用 knowledge_list_scopes；需要按分类、标签、"
                    "来源或连接器收窄时使用 knowledge_list_facets。需要藏知直接"
                    "回答或精确查询表格时使用 knowledge_ask；需要原始证据供外部"
                    "模型自行分析时使用 knowledge_search。需要完整上下文时，再按"
                    "返回的 chunk.id 读取。完整文档必须通过 knowledge_get_document "
                    "按 content_window.next_offset 分页读取，避免大型文档挤占上下文。"
                    "发现表格后先用 knowledge_list_datasets 和 "
                    "knowledge_get_dataset_schema，再用 knowledge_query_dataset 做筛选聚合；"
                    "不要通过预览工具遍历整个数据集。复杂分析应由当前外部 Agent "
                    "组合上述工具完成；MCP 不提供藏知内部 deep 编排，以避免双重 "
                    "Agent 和重复模型消耗。"
                ),
            },
        )
    if payload.method == "ping":
        return _result(payload.id, {})
    if payload.method == "tools/list":
        return _result(payload.id, {"tools": TOOLS})
    if payload.method == "tools/call":
        name = payload.params.get("name")
        arguments = payload.params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return _error(payload.id, -32602, "Invalid params")
        header_access_key = (request.headers.get("X-Cangzhi-Access-Key") or "").strip()
        argument_access_key = arguments.get("access_key")
        if header_access_key and argument_access_key and header_access_key != str(argument_access_key).strip():
            return _error(payload.id, -32602, "请求头与工具参数中的 access_key 不一致")
        if header_access_key and name != "knowledge_list_scopes":
            arguments = {**arguments, "access_key": header_access_key}
        meta = payload.params.get("_meta") or {}
        progress_token = meta.get("progressToken") if isinstance(meta, dict) else None
        accepts_sse = "text/event-stream" in (
            request.headers.get("accept") or ""
        ).lower()
        valid_progress_token = isinstance(progress_token, (str, int)) and not isinstance(
            progress_token, bool
        )
        if (
            name == "knowledge_ask"
            and valid_progress_token
            and accepts_sse
        ):
            return StreamingResponse(
                _stream_ask_result(
                    payload.id,
                    name,
                    arguments,
                    identity,
                    db,
                    progress_token,
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )
        return _result(
            payload.id,
            await _call_tool(name, arguments, identity, db),
        )
    return _error(payload.id, -32601, "Method not found")
