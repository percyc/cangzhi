"""Search API endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_db
from ..models.documents import DocumentSourceType
from ..services.search import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    search_documents,
)


router = APIRouter(prefix="/search", tags=["search"])


class SearchRequest(BaseModel):
    query: Annotated[str, Field(min_length=1, max_length=512)]
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    offset: int = Field(default=0, ge=0, le=10_000)
    category_ids: list[int] = Field(default_factory=list)
    category_slugs: list[str] = Field(default_factory=list)
    tag_ids: list[int] = Field(default_factory=list)
    tag_slugs: list[str] = Field(default_factory=list)
    source_types: list[str] = Field(default_factory=list)

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


def _collect_source_types(
    *,
    body: list[str] | None,
    query_values: list[str] | None,
) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for raw in (body or []) + (query_values or []):
        if not isinstance(raw, str):
            continue
        candidate = raw.strip().lower()
        if candidate and candidate not in seen:
            seen.add(candidate)
            values.append(candidate)
    return values


@router.post("", response_model=dict[str, Any])
async def search_post(
    payload: SearchRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await search_documents(
        db,
        query=payload.query,
        limit=payload.limit,
        offset=payload.offset,
        category_ids=payload.category_ids,
        category_slugs=payload.category_slugs,
        tag_ids=payload.tag_ids,
        tag_slugs=payload.tag_slugs,
        source_types=payload.source_types,
    )
    return result.to_dict()


@router.get("", response_model=dict[str, Any])
async def search_get(
    q: Annotated[str, Query(min_length=1, max_length=512)],
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0, le=10_000),
    category_id: list[int] | None = Query(default=None),
    category_slug: list[str] | None = Query(default=None),
    tag_id: list[int] | None = Query(default=None),
    tag_slug: list[str] | None = Query(default=None),
    source_type: list[str] | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    allowed = {item.value for item in DocumentSourceType}
    cleaned_sources: list[str] = []
    seen_sources: set[str] = set()
    for raw in source_type or []:
        candidate = (raw or "").strip().lower()
        if candidate in allowed and candidate not in seen_sources:
            cleaned_sources.append(candidate)
            seen_sources.add(candidate)
    result = await search_documents(
        db,
        query=q,
        limit=limit,
        offset=offset,
        category_ids=category_id,
        category_slugs=category_slug,
        tag_ids=tag_id,
        tag_slugs=tag_slug,
        source_types=cleaned_sources,
    )
    return result.to_dict()


@router.get("/filters", response_model=dict[str, Any])
async def search_filters(
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Expose the available filter values for the search page."""

    from sqlalchemy import func, select

    from ..models.taxonomy import Category, DocumentCategory, DocumentTag, Tag

    category_rows = (
        await db.execute(
            select(Category.id, Category.slug, Category.name)
            .order_by(Category.sort_order, Category.id)
        )
    ).all()
    category_counts = dict(
        (
            await db.execute(
                select(
                    DocumentCategory.category_id,
                    func.count(func.distinct(DocumentCategory.document_id)),
                ).group_by(DocumentCategory.category_id)
            )
        ).all()
    )
    categories = [
        {
            "id": cat_id,
            "slug": slug,
            "name": name,
            "document_count": int(category_counts.get(cat_id, 0)),
        }
        for cat_id, slug, name in category_rows
    ]

    tag_rows = (
        await db.execute(
            select(Tag.id, Tag.slug, Tag.name)
            .order_by(Tag.slug)
        )
    ).all()
    tag_counts = dict(
        (
            await db.execute(
                select(
                    DocumentTag.tag_id,
                    func.count(func.distinct(DocumentTag.document_id)),
                ).group_by(DocumentTag.tag_id)
            )
        ).all()
    )
    tags = [
        {
            "id": tag_id,
            "slug": slug,
            "name": name,
            "document_count": int(tag_counts.get(tag_id, 0)),
        }
        for tag_id, slug, name in tag_rows
    ]

    return {
        "source_types": [
            {"value": value, "label": _SOURCE_TYPE_LABELS.get(value, value)}
            for value in (item.value for item in DocumentSourceType)
        ],
        "categories": categories,
        "tags": tags,
    }


_SOURCE_TYPE_LABELS = {
    "note": "随手记",
    "file": "文件",
    "url": "链接",
}
