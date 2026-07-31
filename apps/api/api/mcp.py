"""Stateless MCP Streamable HTTP adapter for read-only knowledge tools."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field, ValidationError
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
    list_facet_catalog,
    list_scope_catalog,
)
from ..services.qa import AskError, AskRequest, QAService
from ..services.search import search_documents

router = APIRouter(prefix="/mcp", tags=["mcp"])
_mcp_identity = require_api_identity()
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")


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


class AskArguments(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    scope_id: int | None = Field(default=None, ge=1)
    scope_slug: str | None = Field(default=None, max_length=128)
    category_ids: list[int] = Field(default_factory=list)
    tag_ids: list[int] = Field(default_factory=list)
    source_types: list[str] = Field(default_factory=list)
    connector_ids: list[int] = Field(default_factory=list)
    document_ids: list[int] = Field(default_factory=list)


class DocumentReadArguments(BaseModel):
    document_id: int = Field(ge=1)
    offset: int = Field(default=0, ge=0)
    max_chars: int = Field(default=12_000, ge=1, le=50_000)


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
        "name": "knowledge_search",
        "title": "检索藏知",
        "description": "从藏知检索相关证据片段；适合由外部模型自行组织回答。",
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
            "由藏知检索并回答问题，返回可核验引用。表格的筛选、明细、统计、"
            "分组和排序会优先使用精确计算；需要完整答案时优先使用本工具。"
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
        "inputSchema": {
            "type": "object",
            "properties": {"chunk_id": {"type": "integer", "minimum": 1}},
            "required": ["chunk_id"],
            "additionalProperties": False,
        },
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
                    matches_none=scope.matches_none,
                ),
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
                    **scope.to_filters_dict(),
                },
            }
        )

    forbidden = _require_scope(identity, "knowledge:read")
    if forbidden:
        return forbidden
    try:
        if name == "knowledge_list_scopes":
            return _tool_result({"items": await list_scope_catalog(db)})
        if name == "knowledge_list_facets":
            return _tool_result(await list_facet_catalog(db))
        if name == "knowledge_get_document":
            args = DocumentReadArguments.model_validate(arguments)
            document = await read_current_document(db, args.document_id)
            return _tool_result(_window_document(document, args))
        if name == "knowledge_get_chunk":
            chunk_id = int(arguments.get("chunk_id", 0))
            if chunk_id <= 0:
                raise ValueError
            return _tool_result(await read_current_chunk(db, chunk_id))
    except (TypeError, ValueError, ValidationError):
        return _tool_error(
            "invalid_arguments",
            "读取参数无效，请检查 ID、offset 和 max_chars",
        )
    except KnowledgeReadError as exc:
        return _tool_error(exc.code, str(exc))
    return _tool_error("tool_not_found", f"未知工具：{name}")


@router.post("")
async def mcp_post(
    payload: MCPRequest,
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
        return _result(
            payload.id,
            await _call_tool(name, arguments, identity, db),
        )
    return _error(payload.id, -32601, "Method not found")
