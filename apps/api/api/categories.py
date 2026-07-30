from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai import AIProviderError, build_provider_from_db
from ..core.db import get_db
from ..models.taxonomy import (
    Category,
    DocumentCategory,
    DocumentTag,
    Tag,
    TagMergeRecord,
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
    reason: str | None = Field(default=None, max_length=2000)


class TagMergeBatchGroup(BaseModel):
    target_tag_id: int
    source_tag_ids: list[int] = Field(min_length=1, max_length=50)
    reason: str | None = Field(default=None, max_length=2000)


class TagMergeBatch(BaseModel):
    groups: list[TagMergeBatchGroup] = Field(min_length=1, max_length=50)


class TagMergeSuggestion(BaseModel):
    target_tag_id: int
    source_tag_ids: list[int] = Field(min_length=1, max_length=20)
    reason: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(ge=0, le=1)


class TagMergeSuggestions(BaseModel):
    groups: list[TagMergeSuggestion] = Field(default_factory=list, max_length=50)


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
    await _merge_tag_into(
        db,
        source=source,
        target=target,
        reason=data.reason,
    )
    await db.commit()
    await db.refresh(target)
    return TagResponse.model_validate(target)


async def _merge_tag_into(
    db: AsyncSession,
    *,
    source: Tag,
    target: Tag,
    reason: str | None,
) -> TagMergeRecord:
    links = (
        (
            await db.execute(
                select(DocumentTag).where(DocumentTag.tag_id == source.id)
            )
        )
        .scalars()
        .all()
    )
    moved_version_ids: list[int] = []
    deduplicated_version_ids: list[int] = []
    for link in links:
        existing = await db.scalar(
            select(DocumentTag.id).where(
                DocumentTag.document_version_id == link.document_version_id,
                DocumentTag.tag_id == target.id,
            )
        )
        if existing is not None:
            deduplicated_version_ids.append(link.document_version_id)
            await db.delete(link)
        else:
            moved_version_ids.append(link.document_version_id)
            link.tag_id = target.id
    record = TagMergeRecord(
        target_tag_id=target.id,
        source_snapshot={
            "id": source.id,
            "slug": source.slug,
            "name": source.name,
            "description": source.description,
        },
        moved_version_ids=moved_version_ids,
        deduplicated_version_ids=deduplicated_version_ids,
        status="active",
        reason=reason,
    )
    db.add(record)
    await db.flush()
    await db.delete(source)
    return record


@tags_router.post("/merge-batch", response_model=dict)
async def merge_tags_batch(
    data: TagMergeBatch,
    db: AsyncSession = Depends(get_db),
):
    records: list[int] = []
    used_sources: set[int] = set()
    for group in data.groups:
        target = await db.get(Tag, group.target_tag_id)
        if target is None:
            raise HTTPException(status_code=404, detail="目标标签不存在")
        for source_id in group.source_tag_ids:
            if source_id == target.id or source_id in used_sources:
                raise HTTPException(status_code=400, detail="合并标签存在重复或自身引用")
            source = await db.get(Tag, source_id)
            if source is None:
                raise HTTPException(status_code=404, detail="待合并标签不存在")
            record = await _merge_tag_into(
                db,
                source=source,
                target=target,
                reason=group.reason,
            )
            await db.flush()
            records.append(record.id)
            used_sources.add(source_id)
    await db.commit()
    return {"ok": True, "merged": len(records), "record_ids": records}


@tags_router.post("/suggestions", response_model=dict)
async def suggest_tag_merges(db: AsyncSession = Depends(get_db)):
    tags = list((await db.execute(select(Tag).order_by(Tag.id))).scalars().all())
    if len(tags) < 2:
        return {"mode": "empty", "groups": []}
    provider = await build_provider_from_db(db)
    if provider is None or not provider.is_configured():
        raise HTTPException(
            status_code=503,
            detail="请先在设置中配置可用的对话模型",
        )
    usage = dict(
        (
            await db.execute(
                select(DocumentTag.tag_id, func.count(DocumentTag.id)).group_by(
                    DocumentTag.tag_id
                )
            )
        ).all()
    )
    tag_lines = "\n".join(
        f"- id={tag.id}, 名称={tag.name}, 使用资料数={int(usage.get(tag.id, 0))}"
        for tag in tags[:200]
    )
    prompt = f"""请审查以下个人知识库标签，只建议合并真正同义、简称/全称或明显重复的标签。
相关、上下级、容易混淆但含义不同的标签绝对不要合并。
每组选择一个表达最清晰、使用更稳定的 target_tag_id，其余放入 source_tag_ids。
只输出 JSON：
{{"groups":[{{"target_tag_id":1,"source_tag_ids":[2,3],"reason":"原因","confidence":0.95}}]}}
没有安全建议时输出 {{"groups":[]}}。

标签：
{tag_lines}"""
    try:
        payload = await asyncio.to_thread(
            provider.generate_json,
            system="你是保守的中文知识分类管理员，只输出严格 JSON。",
            prompt=prompt,
        )
        parsed = TagMergeSuggestions.model_validate(payload)
    except (AIProviderError, ValueError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"AI 标签分析失败：{exc}",
        ) from None
    valid_ids = {tag.id for tag in tags}
    used_ids: set[int] = set()
    groups = []
    for group in parsed.groups:
        source_ids = list(dict.fromkeys(group.source_tag_ids))
        all_ids = {group.target_tag_id, *source_ids}
        if (
            group.confidence < 0.7
            or group.target_tag_id not in valid_ids
            or not source_ids
            or any(source_id not in valid_ids for source_id in source_ids)
            or group.target_tag_id in source_ids
            or used_ids.intersection(all_ids)
        ):
            continue
        used_ids.update(all_ids)
        groups.append(group.model_dump())
    return {"mode": "ai", "groups": groups}


@tags_router.get("/merges/history", response_model=list[dict])
async def list_tag_merge_history(db: AsyncSession = Depends(get_db)):
    records = (
        (
            await db.execute(
                select(TagMergeRecord).order_by(TagMergeRecord.id.desc()).limit(100)
            )
        )
        .scalars()
        .all()
    )
    tags = {
        tag.id: tag
        for tag in (
            await db.execute(
                select(Tag).where(
                    Tag.id.in_(
                        [
                            record.target_tag_id
                            for record in records
                            if record.target_tag_id is not None
                        ]
                    )
                )
            )
        ).scalars()
    }
    return [
        {
            "id": record.id,
            "status": record.status,
            "source": record.source_snapshot,
            "target": (
                {
                    "id": tags[record.target_tag_id].id,
                    "name": tags[record.target_tag_id].name,
                }
                if record.target_tag_id in tags
                else None
            ),
            "reason": record.reason,
            "created_at": record.created_at.isoformat()
            if record.created_at
            else None,
            "undone_at": record.undone_at.isoformat()
            if record.undone_at
            else None,
        }
        for record in records
    ]


@tags_router.post("/merges/{record_id}/undo", response_model=TagResponse)
async def undo_tag_merge(
    record_id: int,
    db: AsyncSession = Depends(get_db),
):
    record = await db.get(TagMergeRecord, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="合并记录不存在")
    if record.status != "active":
        raise HTTPException(status_code=409, detail="该合并已经撤销")
    target = (
        await db.get(Tag, record.target_tag_id)
        if record.target_tag_id is not None
        else None
    )
    if target is None:
        raise HTTPException(status_code=409, detail="目标标签已不存在，无法自动撤销")
    snapshot = record.source_snapshot or {}
    slug = str(snapshot.get("slug") or "")
    if not slug or await db.scalar(select(Tag.id).where(Tag.slug == slug)):
        raise HTTPException(status_code=409, detail="原标签标识已被占用，无法自动撤销")
    restored = Tag(
        slug=slug,
        name=str(snapshot.get("name") or "已恢复标签"),
        description=snapshot.get("description"),
    )
    db.add(restored)
    await db.flush()
    moved_ids = set(record.moved_version_ids or [])
    deduplicated_ids = set(record.deduplicated_version_ids or [])
    target_links = (
        (
            await db.execute(
                select(DocumentTag).where(
                    DocumentTag.tag_id == target.id,
                    DocumentTag.document_version_id.in_(
                        list(moved_ids | deduplicated_ids)
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    links_by_version = {link.document_version_id: link for link in target_links}
    for version_id in moved_ids:
        link = links_by_version.get(version_id)
        if link is not None:
            link.tag_id = restored.id
    for version_id in deduplicated_ids:
        link = links_by_version.get(version_id)
        if link is not None:
            db.add(
                DocumentTag(
                    document_id=link.document_id,
                    document_version_id=version_id,
                    tag_id=restored.id,
                    confidence=1.0,
                    source="user",
                )
            )
    record.status = "undone"
    record.undone_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(restored)
    return TagResponse.model_validate(restored)


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
