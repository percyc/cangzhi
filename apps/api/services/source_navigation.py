"""Read-only source navigation service (ADR-024 fifth batch).

Exposes the structured source of the current document version to
REST/MCP/CLI/Skill without ever invoking a model, enqueuing a job,
or writing back to the database. Authorisation, workspace binding
and selection/boundary enforcement are delegated to the shared
``_current_document`` helper used by ``enhancement_read``; this
module never duplicates that logic.

A SQL pre-check on the serialised size of ``structured_content`` is
performed *before* the body is selected, so an
oversized structure can be rejected without ever materialising the
full JSON in Python. The body query repeats that bound; the map is built
in-process via :func:`build_document_map`; oversized block counts
or source text totals are also rejected here. Dataset-only
documents and missing/malformed structures surface as
``structure_unavailable`` so the adapters can recommend the dataset
endpoints instead of pretending the map exists.
"""
from __future__ import annotations

from sqlalchemy import Text, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.documents import DocumentVersion
from . import enhancement_read as auth
from .document_map import build_document_map
from .knowledge_read import KnowledgeReadError
from .scope_keys import DocumentSelection

MAX_SERIALIZED_CHARS = 8_000_000
MAX_BLOCKS = 20_000
MAX_SOURCE_CHARS = 2_000_000
MAX_OFFSET = 10**6
MAX_BLOCK_OFFSET = 2_000_000
MAX_BLOCK_ID = 160
HEADING_TITLE_LIMIT = 200
MAX_MAP_LIMIT = 100
MIN_MAP_LIMIT = 1
DATASET_DOCUMENT_TYPES = frozenset({"xls", "xlsx", "database_table"})
ALLOWED_MAP_VIEWS = frozenset({"outline", "blocks"})

__all__ = [
    "read_document_map",
    "read_document_block",
    "MAX_SERIALIZED_CHARS",
    "MAX_BLOCKS",
    "MAX_SOURCE_CHARS",
    "MAX_OFFSET",
    "MAX_BLOCK_OFFSET",
    "MAX_BLOCK_ID",
    "MAX_MAP_LIMIT",
    "MIN_MAP_LIMIT",
    "ALLOWED_MAP_VIEWS",
    "DATASET_DOCUMENT_TYPES",
]


def _coerce_positive_id(name: str, value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise KnowledgeReadError("invalid_arguments", f"{name} 必须是正整数")
    return value


def _coerce_pagination_int(name: str, value, *, min_value: int, max_value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise KnowledgeReadError("invalid_arguments", f"{name} 必须是整数")
    if value < min_value or value > max_value:
        raise KnowledgeReadError(
            "invalid_arguments", f"{name} 必须在 {min_value}..{max_value} 范围内"
        )
    return value


def _coerce_block_id(value) -> str:
    if not isinstance(value, str) or not value:
        raise KnowledgeReadError("invalid_arguments", "block_id 必须是非空字符串")
    if len(value) > MAX_BLOCK_ID:
        raise KnowledgeReadError(
            "invalid_arguments", f"block_id 最长 {MAX_BLOCK_ID} 个字符"
        )
    return value


def _resolve_view(view) -> str:
    if not isinstance(view, str) or view not in ALLOWED_MAP_VIEWS:
        raise KnowledgeReadError("invalid_arguments", "view 取值无效")
    return view


def _slice_block_text(
    payload_blocks: list[dict],
    entry: dict,
    *,
    offset: int,
    max_chars: int,
    source_fingerprint: str,
) -> dict:
    """Return the same payload as :func:`read_map_block` without rebuilding the map."""
    text = payload_blocks[entry["block_index"]].get("text", "")
    if not isinstance(text, str):
        text = ""
    if offset > len(text):
        raise KnowledgeReadError("invalid_arguments", "offset 超出原文长度")
    end = min(offset + max_chars, len(text))
    return {
        **entry,
        "text": text[offset:end],
        "offset": offset,
        "next_offset": end if end < len(text) else None,
        "partial": offset > 0 or end < len(text),
        "source_fingerprint": source_fingerprint,
    }


async def _serialised_length(db: AsyncSession, version_id: int) -> int | None:
    """Return the textual length of ``structured_content`` or ``None`` if absent.

    Performed via SQL so the body is never pulled into Python.
    """
    length = await db.scalar(
        select(func.length(cast(DocumentVersion.structured_content, Text)))
        .where(DocumentVersion.id == version_id)
    )
    if length is None:
        return None
    return int(length)


async def _load_bounded_structure(db: AsyncSession, version_id: int):
    # Repeat the size predicate in the body query: reparsing may update a version
    # between the initial length check and this read. Do not materialize an
    # oversized replacement, and do not assign a read projection back to the ORM.
    row = (await db.execute(select(DocumentVersion.structured_content).where(
        DocumentVersion.id == version_id,
        func.length(cast(DocumentVersion.structured_content, Text)) <= MAX_SERIALIZED_CHARS,
    ))).first()
    if row is None:
        raise KnowledgeReadError("source_changed", "来源在读取期间变更，请重新读取文档地图")
    return row[0]


def _build_authorised_map(payload, version_id: int) -> tuple[dict, dict]:
    """Validate the bounded body, build the map and enforce size limits.

    Returns ``(payload, document_map)`` for the block reader. Raises
    :class:`KnowledgeReadError` with the right code when the
    document is missing structure, is dataset-only, malformed or
    exceeds the configured size limits.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("blocks"), list):
        raise KnowledgeReadError(
            "structure_unavailable", "原文结构不可用，请使用基础检索或数据集工具"
        )
    document_type = payload.get("document_type", "")
    if isinstance(document_type, str) and document_type in DATASET_DOCUMENT_TYPES:
        raise KnowledgeReadError(
            "structure_unavailable", "该文档为数据集，请使用数据集查询工具"
        )
    if len(payload["blocks"]) > MAX_BLOCKS:
        raise KnowledgeReadError("structure_too_large", "原文块数超过读取上限，请使用分页原文读取")
    try:
        document_map = build_document_map(payload, version_id=version_id)
    except (TypeError, ValueError) as exc:
        raise KnowledgeReadError(
            "structure_unavailable", "原文结构已损坏，请重新解析或使用基础检索"
        ) from exc
    if len(document_map["blocks"]) > MAX_BLOCKS:
        raise KnowledgeReadError(
            "structure_too_large",
            f"原文块数超过上限 {MAX_BLOCKS}，请使用分页原文读取或数据集查询",
        )
    total_source = sum(entry["char_count"] for entry in document_map["blocks"])
    if total_source > MAX_SOURCE_CHARS:
        raise KnowledgeReadError(
            "structure_too_large",
            f"原文总字符数超过上限 {MAX_SOURCE_CHARS}，请使用分页原文读取",
        )
    return payload, document_map


def _paginate(items: list, offset: int, limit: int) -> tuple[list, int, int | None]:
    total = len(items)
    sliced = items[offset:offset + limit]
    next_offset = offset + len(sliced) if offset + len(sliced) < total else None
    return sliced, total, next_offset


def _decorate_heading(entry: dict, payload_blocks: list[dict]) -> dict:
    text = payload_blocks[entry["block_index"]].get("text", "")
    if not isinstance(text, str):
        text = ""
    truncated = len(text) > HEADING_TITLE_LIMIT
    if truncated:
        text = text[:HEADING_TITLE_LIMIT]
    return {**entry, "title": text, "title_truncated": truncated}


def _outline_items(document_map: dict, payload_blocks: list[dict]) -> list[dict]:
    return [
        _decorate_heading(entry, payload_blocks)
        for entry in document_map["blocks"]
        if entry["type"] == "heading"
    ]


def _block_items(document_map: dict) -> list[dict]:
    return document_map["blocks"]


def _bounded_metadata(entry: dict) -> dict:
    """Path labels are navigation hints, not an unbounded second body field."""
    path = entry["heading_path"]
    return {**entry, "heading_path": [part[:200] for part in path[:16]],
            "heading_path_truncated": len(path) > 16 or any(len(part) > 200 for part in path)}


async def read_document_map(
    db: AsyncSession,
    document_id: int,
    *,
    view: str = "outline",
    offset: int = 0,
    limit: int = 20,
    document_selection: DocumentSelection | None = None,
    document_boundary: DocumentSelection | None = None,
) -> dict:
    """Return a paginated outline or block list for the current version.

    The function never invokes a model, never enqueues jobs and
    never commits. A SQL pre-check on the serialised length of
    ``structured_content`` happens before the body is selected, so
    oversized structures are rejected without materialising them.
    """
    _coerce_positive_id("document_id", document_id)
    view = _resolve_view(view)
    offset = _coerce_pagination_int(
        "offset", offset, min_value=0, max_value=MAX_OFFSET
    )
    limit = _coerce_pagination_int(
        "limit", limit, min_value=MIN_MAP_LIMIT, max_value=MAX_MAP_LIMIT
    )

    document, version = await auth._current_document(
        db,
        document_id,
        document_selection=document_selection,
        document_boundary=document_boundary,
    )
    serialised = await _serialised_length(db, version.id)
    if serialised is None:
        raise KnowledgeReadError(
            "structure_unavailable", "原文结构不可用，请使用基础检索或数据集工具"
        )
    if serialised > MAX_SERIALIZED_CHARS:
        raise KnowledgeReadError(
            "structure_too_large",
            "原文结构超过读取上限，请使用分页原文读取或数据集查询",
        )
    payload, document_map = _build_authorised_map(await _load_bounded_structure(db, version.id), version.id)
    payload_blocks = payload["blocks"]
    if view == "outline":
        items_source = _outline_items(document_map, payload_blocks)
    else:
        items_source = _block_items(document_map)
    sliced, total, next_offset = _paginate(items_source, offset, limit)
    return {
        "document_id": document.id,
        "document_version_id": version.id,
        "source_fingerprint": document_map["source_fingerprint"],
        "view": view,
        "items": [_bounded_metadata(entry) for entry in sliced],
        "total": total,
        "offset": offset,
        "limit": limit,
        "next_offset": next_offset,
        "read_only": True,
        "model_calls": 0,
    }


async def read_document_block(
    db: AsyncSession,
    document_id: int,
    *,
    block_id: str,
    offset: int = 0,
    max_chars: int = 4000,
    document_selection: DocumentSelection | None = None,
    document_boundary: DocumentSelection | None = None,
) -> dict:
    """Return exact source text for a single block from the current map.

    The block id is verified against the freshly built map so a
    forged id from an older fingerprint or a different version
    raises ``source_changed``; the adapter converts that to 409 so
    callers can re-read the map. The exact raw text, offset,
    next_offset and partial metadata mirror :func:`read_map_block`.
    """
    _coerce_positive_id("document_id", document_id)
    block_id = _coerce_block_id(block_id)
    offset = _coerce_pagination_int(
        "offset", offset, min_value=0, max_value=MAX_BLOCK_OFFSET
    )
    max_chars = _coerce_pagination_int(
        "max_chars", max_chars, min_value=1, max_value=12000
    )

    document, version = await auth._current_document(
        db,
        document_id,
        document_selection=document_selection,
        document_boundary=document_boundary,
    )
    serialised = await _serialised_length(db, version.id)
    if serialised is None:
        raise KnowledgeReadError(
            "structure_unavailable", "原文结构不可用，请使用基础检索或数据集工具"
        )
    if serialised > MAX_SERIALIZED_CHARS:
        raise KnowledgeReadError(
            "structure_too_large",
            "原文结构超过读取上限，请使用分页原文读取或数据集查询",
        )
    payload, document_map = _build_authorised_map(await _load_bounded_structure(db, version.id), version.id)
    entry = next((b for b in document_map["blocks"] if b["id"] == block_id), None)
    if entry is None:
        raise KnowledgeReadError(
            "source_changed",
            "来源已变更，请重新读取文档地图",
        )
    block = _slice_block_text(
        payload["blocks"],
        entry,
        offset=offset,
        max_chars=max_chars,
        source_fingerprint=document_map["source_fingerprint"],
    )
    return {
        "document_id": document.id,
        "document_version_id": version.id,
        "source_fingerprint": document_map["source_fingerprint"],
        "block": _bounded_metadata(block),
        "read_only": True,
        "model_calls": 0,
    }
