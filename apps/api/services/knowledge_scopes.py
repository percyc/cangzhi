"""Canonical knowledge-scope validation, merging, and resolution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.documents import Document, DocumentSourceType
from ..models.knowledge_scopes import KnowledgeScope
from ..models.taxonomy import Category, DocumentCategory, DocumentTag, Tag
from ..models.webdav import WebDAVEntry, WebDAVSource
from .scope_keys import DocumentSelection, candidate_condition

FILTER_DIMENSIONS = (
    "category_ids",
    "tag_ids",
    "source_types",
    "connector_ids",
    "document_ids",
)
_ID_DIMENSIONS = frozenset(FILTER_DIMENSIONS) - {"source_types"}
_SOURCE_TYPES = frozenset(item.value for item in DocumentSourceType)

SYSTEM_SCOPE_FILTERS: dict[str, dict[str, list[Any]]] = {
    "all": {},
    "notes": {"source_types": ["note"]},
    "web": {"source_types": ["url"]},
    "files": {"source_types": ["file"]},
}

# Kept as named constants for adapters and tests.
SYSTEM_SCOPE_ALL = "all"
SYSTEM_SCOPE_NOTES = "notes"
SYSTEM_SCOPE_WEB = "web"
SYSTEM_SCOPE_FILES = "files"
SYSTEM_SCOPES = frozenset(SYSTEM_SCOPE_FILTERS)
SYSTEM_SCOPE_NAMES = {
    "all": "全部知识",
    "notes": "随手记",
    "web": "网页收藏",
    "files": "文件资料",
}
SOURCE_TYPE_NAMES = {
    DocumentSourceType.note.value: "随手记",
    DocumentSourceType.url.value: "网页收藏",
    DocumentSourceType.file.value: "文件资料",
}


class KnowledgeScopeError(ValueError):
    """Base error carrying a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class KnowledgeScopeNotFound(KnowledgeScopeError):
    def __init__(self):
        super().__init__("scope_not_found", "知识范围不存在")


@dataclass
class ResolvedScope:
    """Validated filters ready for retrieval.

    ``matches_none`` is deliberately separate from empty filter lists:
    empty lists mean unrestricted in the legacy retrieval services, while
    an empty intersection must mean no results.
    """

    category_ids: list[int] = field(default_factory=list)
    tag_ids: list[int] = field(default_factory=list)
    source_types: list[str] = field(default_factory=list)
    connector_ids: list[int] = field(default_factory=list)
    document_ids: list[int] = field(default_factory=list)
    matches_none: bool = False
    scope_id: int | None = None
    scope_slug: str = SYSTEM_SCOPE_ALL

    def to_filters_dict(self) -> dict[str, Any]:
        return {
            "category_ids": list(self.category_ids),
            "tag_ids": list(self.tag_ids),
            "source_types": list(self.source_types),
            "connector_ids": list(self.connector_ids),
            "document_ids": list(self.document_ids),
            "matches_none": self.matches_none,
        }


def _normalize_ids(value: Any, *, dimension: str) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise KnowledgeScopeError(
            "invalid_scope_filter", f"{dimension} 必须是整数数组"
        )
    normalized: set[int] = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise KnowledgeScopeError(
                "invalid_scope_filter", f"{dimension} 只能包含正整数"
            )
        normalized.add(item)
    return sorted(normalized)


def normalize_filter_input(
    value: Any, *, dimension: str = "document_ids"
) -> list[int]:
    """Backward-compatible public helper for normalizing ID dimensions."""

    if isinstance(value, int) and not isinstance(value, bool):
        value = [value]
    return _normalize_ids(value, dimension=dimension)


def _normalize_source_types(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise KnowledgeScopeError(
            "invalid_scope_filter", "source_types 必须是来源类型数组"
        )
    normalized: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise KnowledgeScopeError(
                "invalid_scope_filter", "source_types 只能包含字符串"
            )
        candidate = item.strip().lower()
        if candidate not in _SOURCE_TYPES:
            raise KnowledgeScopeError(
                "invalid_scope_filter", f"不支持的来源类型：{candidate or '(空)'}"
            )
        normalized.add(candidate)
    return sorted(normalized)


def normalize_scope_filter(value: Mapping[str, Any] | None) -> dict[str, list[Any]]:
    """Validate the scope filter whitelist and return canonical lists."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise KnowledgeScopeError("invalid_scope_filter", "知识范围过滤条件必须是对象")
    unknown = sorted(set(value) - set(FILTER_DIMENSIONS))
    if unknown:
        raise KnowledgeScopeError(
            "invalid_scope_filter", f"不支持的过滤字段：{', '.join(unknown)}"
        )
    normalized: dict[str, list[Any]] = {}
    for dimension in FILTER_DIMENSIONS:
        if dimension not in value:
            continue
        items = (
            _normalize_source_types(value[dimension])
            if dimension == "source_types"
            else _normalize_ids(value[dimension], dimension=dimension)
        )
        # An empty array means "no constraint", consistent with the current UI.
        if items:
            normalized[dimension] = items
    return normalized


def intersect_lists(left: Sequence[int], right: Sequence[int]) -> list[int]:
    return sorted(set(left).intersection(right))


def merge_filters(
    saved: Mapping[str, Any] | None,
    temporary: Mapping[str, Any] | None,
) -> ResolvedScope:
    """Merge filters; the same dimension intersects and can never broaden."""

    left = normalize_scope_filter(saved)
    right = normalize_scope_filter(temporary)
    merged: dict[str, list[Any]] = {}
    matches_none = False
    for dimension in FILTER_DIMENSIONS:
        left_values = left.get(dimension)
        right_values = right.get(dimension)
        if left_values and right_values:
            values = sorted(set(left_values).intersection(right_values))
            if not values:
                matches_none = True
        else:
            values = list(left_values or right_values or [])
        merged[dimension] = values
    return ResolvedScope(
        category_ids=merged["category_ids"],
        tag_ids=merged["tag_ids"],
        source_types=merged["source_types"],
        connector_ids=merged["connector_ids"],
        document_ids=merged["document_ids"],
        matches_none=matches_none,
    )


async def resolve_connector_document_ids(
    db: AsyncSession, connector_ids: Sequence[int]
) -> list[int]:
    """Return active documents linked to any selected WebDAV connector."""

    if not connector_ids:
        return []
    rows = (
        await db.execute(
            select(WebDAVEntry.document_id)
            .join(Document, Document.id == WebDAVEntry.document_id)
            .where(
                WebDAVEntry.source_id.in_(connector_ids),
                WebDAVEntry.document_id.is_not(None),
                Document.is_deleted.is_(False),
            )
            .distinct()
        )
    ).scalars()
    return sorted(item for item in rows if item is not None)


class KnowledgeScopeResolver:
    """Resolve a saved/system scope and optional request-time narrowing."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def resolve(
        self,
        *,
        scope_id: int | None = None,
        scope_slug: str | None = None,
        category_ids: Sequence[int] | None = None,
        tag_ids: Sequence[int] | None = None,
        source_types: Sequence[str] | None = None,
        connector_ids: Sequence[int] | None = None,
        document_ids: Sequence[int] | None = None,
    ) -> ResolvedScope:
        if scope_id is not None and scope_slug is not None:
            raise KnowledgeScopeError(
                "invalid_scope", "scope_id 与 scope_slug 不能同时提供"
            )

        resolved_slug = (scope_slug or SYSTEM_SCOPE_ALL).strip().lower()
        saved_filter: Mapping[str, Any]
        saved_id: int | None = None
        if scope_id is None and resolved_slug in SYSTEM_SCOPE_FILTERS:
            saved_filter = SYSTEM_SCOPE_FILTERS[resolved_slug]
        elif scope_id is not None or scope_slug is not None:
            scope = await self._load_saved_scope(scope_id, scope_slug)
            if scope is None:
                raise KnowledgeScopeNotFound()
            saved_filter = scope.filter or {}
            saved_id = scope.id
            resolved_slug = scope.slug
        else:
            saved_filter = {}

        temporary = {
            key: value
            for key, value in {
                "category_ids": category_ids,
                "tag_ids": tag_ids,
                "source_types": source_types,
                "connector_ids": connector_ids,
                "document_ids": document_ids,
            }.items()
            if value
        }
        resolved = merge_filters(saved_filter, temporary)
        resolved.scope_id = saved_id
        resolved.scope_slug = resolved_slug

        if resolved.matches_none:
            return resolved

        if resolved.document_ids:
            active_ids = await self._active_document_ids(resolved.document_ids)
            if not active_ids:
                resolved.matches_none = True
            resolved.document_ids = active_ids

        if resolved.connector_ids:
            connector_docs = await resolve_connector_document_ids(
                self.db, resolved.connector_ids
            )
            if not connector_docs:
                resolved.matches_none = True
                resolved.document_ids = []
            elif resolved.document_ids:
                resolved.document_ids = intersect_lists(
                    resolved.document_ids, connector_docs
                )
                if not resolved.document_ids:
                    resolved.matches_none = True
            else:
                resolved.document_ids = connector_docs

        return resolved

    async def _load_saved_scope(
        self, scope_id: int | None, scope_slug: str | None
    ) -> KnowledgeScope | None:
        if scope_id is not None:
            condition = KnowledgeScope.id == scope_id
        else:
            condition = KnowledgeScope.slug == (scope_slug or "").strip().lower()
        return (
            await self.db.execute(select(KnowledgeScope).where(condition))
        ).scalar_one_or_none()

    async def _active_document_ids(self, document_ids: Sequence[int]) -> list[int]:
        return sorted(
            (
                await self.db.execute(
                    select(Document.id).where(
                        Document.id.in_(document_ids),
                        Document.is_deleted.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )

    async def validate_document_ids(self, document_ids: list[int]) -> list[int]:
        return await self._active_document_ids(document_ids)


async def list_scope_catalog(db: AsyncSession) -> list[dict[str, Any]]:
    """Return system and saved scopes in the adapter-neutral public shape."""

    saved = (
        (
            await db.execute(
                select(KnowledgeScope).order_by(
                    KnowledgeScope.name, KnowledgeScope.id
                )
            )
        )
        .scalars()
        .all()
    )
    system = [
        {
            "id": None,
            "slug": slug,
            "name": SYSTEM_SCOPE_NAMES[slug],
            "description": None,
            "filter": filters,
            "version": 1,
            "system": True,
        }
        for slug, filters in SYSTEM_SCOPE_FILTERS.items()
    ]
    return system + [
        {**scope.to_public_dict(), "system": False} for scope in saved
    ]


async def list_facet_catalog(
    db: AsyncSession,
    *,
    document_selection: DocumentSelection | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return filter choices and active-document counts for every adapter.

    ``document_selection`` is the union of scope-key-bound documents
    and explicit document ids. When provided, the facet counts only
    include documents that intersect with the selection, mirroring the
    Search/Ask/Deep contract.
    """

    selection_condition = candidate_condition(document_selection) if document_selection else None

    category_rows = (
        await db.execute(
            select(
                Category,
                func.count(func.distinct(Document.id)).label("document_count"),
            )
            .outerjoin(
                DocumentCategory,
                DocumentCategory.category_id == Category.id,
            )
            .outerjoin(
                Document,
                and_(
                    Document.id == DocumentCategory.document_id,
                    Document.is_deleted.is_(False),
                    Document.current_version_id
                    == DocumentCategory.document_version_id,
                    selection_condition if selection_condition is not None else True,
                ),
            )
            .group_by(Category.id)
            .order_by(Category.sort_order, Category.name, Category.id)
        )
    ).all()
    tag_rows = (
        await db.execute(
            select(
                Tag,
                func.count(func.distinct(Document.id)).label("document_count"),
            )
            .outerjoin(DocumentTag, DocumentTag.tag_id == Tag.id)
            .outerjoin(
                Document,
                and_(
                    Document.id == DocumentTag.document_id,
                    Document.is_deleted.is_(False),
                    Document.current_version_id == DocumentTag.document_version_id,
                    selection_condition if selection_condition is not None else True,
                ),
            )
            .group_by(Tag.id)
            .order_by(
                func.count(func.distinct(Document.id)).desc(),
                Tag.name,
                Tag.id,
            )
        )
    ).all()
    source_counts = {
        source_type.value
        if isinstance(source_type, DocumentSourceType)
        else str(source_type): int(count)
        for source_type, count in (
            await db.execute(
                select(Document.source_type, func.count(Document.id))
                .where(
                    Document.is_deleted.is_(False),
                    selection_condition if selection_condition is not None else True,
                )
                .group_by(Document.source_type)
            )
        ).all()
    }
    connector_rows = (
        await db.execute(
            select(
                WebDAVSource,
                func.count(func.distinct(Document.id)).label("document_count"),
            )
            .outerjoin(WebDAVEntry, WebDAVEntry.source_id == WebDAVSource.id)
            .outerjoin(
                Document,
                and_(
                    Document.id == WebDAVEntry.document_id,
                    Document.is_deleted.is_(False),
                    selection_condition if selection_condition is not None else True,
                ),
            )
            .group_by(WebDAVSource.id)
            .order_by(WebDAVSource.name, WebDAVSource.id)
        )
    ).all()
    has_selection = document_selection is not None
    return {
        "categories": [
            {
                "id": category.id,
                "slug": category.slug,
                "name": category.name,
                "parent_id": category.parent_id,
                "document_count": int(document_count),
            }
            for category, document_count in category_rows
            if not has_selection or int(document_count) > 0
        ],
        "tags": [
            {
                "id": tag.id,
                "slug": tag.slug,
                "name": tag.name,
                "document_count": int(document_count),
            }
            for tag, document_count in tag_rows
            if not has_selection or int(document_count) > 0
        ],
        "source_types": [
            {
                "value": source_type.value,
                "label": SOURCE_TYPE_NAMES[source_type.value],
                "document_count": source_counts.get(source_type.value, 0),
            }
            for source_type in DocumentSourceType
            if not has_selection or source_counts.get(source_type.value, 0) > 0
        ],
        "connectors": [
            {
                "id": source.id,
                "name": source.name,
                "is_enabled": bool(source.is_enabled),
                "document_count": int(document_count),
            }
            for source, document_count in connector_rows
            if not has_selection or int(document_count) > 0
        ],
    }
