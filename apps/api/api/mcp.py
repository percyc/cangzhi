"""Stateless MCP Streamable HTTP adapter for read-only knowledge tools."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

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
        "name": "knowledge_get_document",
        "title": "读取知识文档",
        "description": "按文档 ID 读取当前有效版本和原始正文。",
        "inputSchema": {
            "type": "object",
            "properties": {"document_id": {"type": "integer", "minimum": 1}},
            "required": ["document_id"],
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
        "content": [
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}
        ],
        "structuredContent": payload,
        "isError": True,
    }


def _require_scope(identity: APIIdentity, scope: str) -> dict | None:
    if identity.has_scope(scope):
        return None
    return _tool_error("insufficient_scope", f"令牌缺少权限：{scope}")


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

    forbidden = _require_scope(identity, "knowledge:read")
    if forbidden:
        return forbidden
    try:
        if name == "knowledge_list_scopes":
            return _tool_result({"items": await list_scope_catalog(db)})
        if name == "knowledge_get_document":
            document_id = int(arguments.get("document_id", 0))
            if document_id <= 0:
                raise ValueError
            return _tool_result(await read_current_document(db, document_id))
        if name == "knowledge_get_chunk":
            chunk_id = int(arguments.get("chunk_id", 0))
            if chunk_id <= 0:
                raise ValueError
            return _tool_result(await read_current_chunk(db, chunk_id))
    except (TypeError, ValueError):
        return _tool_error("invalid_arguments", "ID 必须是正整数")
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
                "serverInfo": {"name": "cangzhi", "version": "0.1.0"},
                "instructions": (
                    "优先使用 knowledge_search 获取证据；需要完整上下文时，"
                    "再按返回的 document_id 或 chunk.id 读取。"
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
