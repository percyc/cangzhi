from __future__ import annotations

import hashlib
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_db
from ..models.taxonomy import (
    Category,
    DocumentCategory,
    DocumentTag,
    Tag,
)

router = APIRouter(prefix="/categories", tags=["categories"])


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def _validate_slug(value: str) -> str:
    if not _SLUG_RE.fullmatch(value):
        raise ValueError(
            "slug 只能包含小写字母、数字和短横线，且必须以字母或数字开头"
        )
    return value


class CategoryCreate(BaseModel):
    slug: Annotated[str | None, Field(default=None, min_length=1, max_length=64)]
    name: Annotated[str, Field(min_length=1, max_length=255)]
    description: str | None = Field(default=None, max_length=2000)
    parent_id: int | None = None
    sort_order: int = 0

    @field_validator("slug")
    @classmethod
    def _slug_format(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = (value or "").strip().lower()
        return _validate_slug(cleaned)


class CategoryUpdate(BaseModel):
    name: Annotated[str | None, Field(default=None, min_length=1, max_length=255)]
    description: str | None = Field(default=None, max_length=2000)
    parent_id: int | None = None
    sort_order: int | None = None


class CategoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    description: str | None
    sort_order: int
    is_default: bool
    parent_id: int | None
    document_count: int = 0


def _to_response(category: Category, document_count: int) -> CategoryResponse:
    return CategoryResponse(
        id=category.id,
        slug=category.slug,
        name=category.name,
        description=category.description,
        sort_order=category.sort_order,
        is_default=category.is_default,
        parent_id=category.parent_id,
        document_count=document_count,
    )


@router.get("", response_model=list[CategoryResponse])
async def list_categories(db: AsyncSession = Depends(get_db)):
    categories = list(
        (
            await db.execute(
                select(Category).order_by(Category.sort_order, Category.id)
            )
        )
        .scalars()
        .all()
    )
    if not categories:
        return []
    counter = dict(
        (
            await db.execute(
                select(
                    DocumentCategory.category_id,
                    func.count(func.distinct(DocumentCategory.document_id)),
                ).group_by(DocumentCategory.category_id)
            )
        ).all()
    )
    return [_to_response(cat, counter.get(cat.id, 0)) for cat in categories]


@router.get("/{category_id}", response_model=CategoryResponse)
async def get_category(category_id: int, db: AsyncSession = Depends(get_db)):
    category = await db.get(Category, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="分类不存在")
    result = await db.execute(
        select(DocumentCategory.category_id)
        .where(DocumentCategory.category_id == category_id)
        .distinct()
    )
    count = len(result.scalars().all())
    return _to_response(category, count)


@router.post("", response_model=CategoryResponse, status_code=201)
async def create_category(
    data: CategoryCreate,
    db: AsyncSession = Depends(get_db),
):
    slug = data.slug or f"category-{hashlib.sha1(data.name.strip().encode()).hexdigest()[:12]}"
    existing = await db.scalar(select(Category).where(Category.slug == slug))
    if existing is not None:
        raise HTTPException(status_code=409, detail="分类 slug 已存在")
    if data.parent_id is not None:
        parent = await db.get(Category, data.parent_id)
        if parent is None:
            raise HTTPException(status_code=400, detail="父分类不存在")
    category = Category(
        slug=slug,
        name=data.name.strip(),
        description=data.description,
        parent_id=data.parent_id,
        sort_order=data.sort_order,
    )
    db.add(category)
    await db.commit()
    await db.refresh(category)
    return _to_response(category, 0)


@router.patch("/{category_id}", response_model=CategoryResponse)
async def update_category(
    category_id: int,
    data: CategoryUpdate,
    db: AsyncSession = Depends(get_db),
):
    category = await db.get(Category, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="分类不存在")
    if data.name is not None:
        category.name = data.name.strip()
    if data.description is not None:
        category.description = data.description
    if data.sort_order is not None:
        category.sort_order = data.sort_order
    if data.parent_id is not None:
        if data.parent_id == category.id:
            raise HTTPException(status_code=400, detail="父分类不能指向自身")
        parent = await db.get(Category, data.parent_id)
        if parent is None:
            raise HTTPException(status_code=400, detail="父分类不存在")
        # Disallow cycles by walking up the parent chain.
        cursor = parent
        while cursor is not None:
            if cursor.id == category.id:
                raise HTTPException(
                    status_code=400, detail="父分类不能形成循环"
                )
            if cursor.parent_id is None:
                break
            cursor = await db.get(Category, cursor.parent_id)
        category.parent_id = data.parent_id
    db.add(category)
    await db.commit()
    await db.refresh(category)
    return _to_response(category, 0)


@router.delete("/{category_id}", status_code=204)
async def delete_category(category_id: int, db: AsyncSession = Depends(get_db)):
    category = await db.get(Category, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="分类不存在")

    has_documents = (
        await db.scalar(
            select(DocumentCategory.id)
            .where(DocumentCategory.category_id == category_id)
            .limit(1)
        )
    ) is not None
    if has_documents:
        raise HTTPException(
            status_code=409,
            detail="该分类仍有资料，请先将资料迁移到其他分类后再删除",
        )

    has_children = (
        await db.scalar(
            select(Category.id)
            .where(Category.parent_id == category_id)
            .limit(1)
        )
    ) is not None
    if has_children:
        raise HTTPException(
            status_code=409,
            detail="该分类下仍有子分类，请先删除或迁移子分类",
        )

    await db.delete(category)
    await db.commit()


@router.get("/{category_id}/documents", response_model=list[int])
async def list_category_documents(
    category_id: int,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    category = await db.get(Category, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="分类不存在")
    result = await db.execute(
        select(DocumentCategory.document_id)
        .where(DocumentCategory.category_id == category_id)
        .order_by(DocumentCategory.id.desc())
        .limit(limit)
    )
    return [row[0] for row in result.all()]


# Tag endpoints -------------------------------------------------------------


class TagCreate(BaseModel):
    slug: Annotated[str | None, Field(default=None, min_length=1, max_length=64)]
    name: Annotated[str, Field(min_length=1, max_length=255)]
    description: str | None = Field(default=None, max_length=2000)

    @field_validator("slug")
    @classmethod
    def _slug_format(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = (value or "").strip().lower()
        return _validate_slug(cleaned)


class TagUpdate(BaseModel):
    name: Annotated[str | None, Field(default=None, min_length=1, max_length=255)]
    description: str | None = Field(default=None, max_length=2000)


class TagMerge(BaseModel):
    target_tag_id: int


class TagResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    description: str | None


tags_router = APIRouter(prefix="/tags", tags=["tags"])


@tags_router.get("", response_model=list[TagResponse])
async def list_tags(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Tag).order_by(Tag.slug))
    return [TagResponse.model_validate(tag) for tag in result.scalars().all()]


@tags_router.post("", response_model=TagResponse, status_code=201)
async def create_tag(data: TagCreate, db: AsyncSession = Depends(get_db)):
    slug = data.slug or f"tag-{hashlib.sha1(data.name.strip().encode()).hexdigest()[:12]}"
    existing = await db.scalar(select(Tag).where(Tag.slug == slug))
    if existing is not None:
        raise HTTPException(status_code=409, detail="标签 slug 已存在")
    tag = Tag(
        slug=slug,
        name=data.name.strip(),
        description=data.description,
    )
    db.add(tag)
    await db.commit()
    await db.refresh(tag)
    return TagResponse.model_validate(tag)


@tags_router.patch("/{tag_id}", response_model=TagResponse)
async def update_tag(
    tag_id: int,
    data: TagUpdate,
    db: AsyncSession = Depends(get_db),
):
    tag = await db.get(Tag, tag_id)
    if tag is None:
        raise HTTPException(status_code=404, detail="标签不存在")
    if data.name is not None:
        tag.name = data.name.strip()
    if "description" in data.model_fields_set:
        tag.description = data.description
    await db.commit()
    await db.refresh(tag)
    return TagResponse.model_validate(tag)


@tags_router.post("/{tag_id}/merge", response_model=TagResponse)
async def merge_tag(
    tag_id: int,
    data: TagMerge,
    db: AsyncSession = Depends(get_db),
):
    if tag_id == data.target_tag_id:
        raise HTTPException(status_code=400, detail="不能合并到自身")
    source = await db.get(Tag, tag_id)
    target = await db.get(Tag, data.target_tag_id)
    if source is None or target is None:
        raise HTTPException(status_code=404, detail="标签不存在")
    links = (
        (
            await db.execute(
                select(DocumentTag).where(DocumentTag.tag_id == source.id)
            )
        )
        .scalars()
        .all()
    )
    for link in links:
        existing = await db.scalar(
            select(DocumentTag.id).where(
                DocumentTag.document_version_id == link.document_version_id,
                DocumentTag.tag_id == target.id,
            )
        )
        if existing is not None:
            await db.delete(link)
        else:
            link.tag_id = target.id
    await db.flush()
    await db.delete(source)
    await db.commit()
    await db.refresh(target)
    return TagResponse.model_validate(target)


@tags_router.delete("/{tag_id}", status_code=204)
async def delete_tag(tag_id: int, db: AsyncSession = Depends(get_db)):
    tag = await db.get(Tag, tag_id)
    if tag is None:
        raise HTTPException(status_code=404, detail="标签不存在")
    has_links = (
        await db.scalar(
            select(DocumentTag.id).where(DocumentTag.tag_id == tag_id).limit(1)
        )
    ) is not None
    if has_links:
        raise HTTPException(
            status_code=409,
            detail="该标签仍被资料使用，请先解除关联",
        )
    await db.delete(tag)
    await db.commit()
