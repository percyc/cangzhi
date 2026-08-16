"""Lightweight document retrieval ranges for trusted integrations."""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import HTTPException
from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.document_access_keys import DocumentAccessKey
from ..models.documents import Document

MAX_ACCESS_KEYS = 100
MAX_ACCESS_KEY_LENGTH = 128


def normalize_access_key(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("access_key 不能为空")
    if len(cleaned) > MAX_ACCESS_KEY_LENGTH:
        raise ValueError(f"access_key 最长 {MAX_ACCESS_KEY_LENGTH} 个字符")
    return cleaned


def normalize_access_keys(values: Sequence[str] | None) -> list[str]:
    if not values:
        return []
    normalized = sorted({normalize_access_key(item) for item in values})
    result = [item for item in normalized if item is not None]
    if len(result) > MAX_ACCESS_KEYS:
        raise ValueError(f"每篇文档最多设置 {MAX_ACCESS_KEYS} 个 access key")
    return result


def resolve_access_key(header_value: str | None, body_value: str | None) -> str | None:
    try:
        header_key = normalize_access_key(header_value)
        body_key = normalize_access_key(body_value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_access_key", "message": str(exc)},
        ) from None
    if header_key and body_key and header_key != body_key:
        raise HTTPException(
            status_code=400,
            detail={"code": "access_key_conflict", "message": "请求头与参数中的 access_key 不一致"},
        )
    return header_key or body_key


def document_has_access_key(access_key: str):
    return exists(
        select(DocumentAccessKey.id).where(
            DocumentAccessKey.document_id == Document.id,
            DocumentAccessKey.workspace_id == Document.workspace_id,
            DocumentAccessKey.access_key == access_key,
        )
    )


async def ensure_document_visible(
    db: AsyncSession, document_id: int, access_key: str | None
) -> None:
    if access_key is None:
        return
    visible = await db.scalar(
        select(Document.id).where(
            Document.id == document_id,
            Document.is_deleted.is_(False),
            document_has_access_key(access_key),
        )
    )
    if visible is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "document_not_found", "message": "知识文档不存在"},
        )


async def list_document_access_keys(db: AsyncSession, document_id: int) -> list[str]:
    return list(
        (
            await db.scalars(
                select(DocumentAccessKey.access_key)
                .where(DocumentAccessKey.document_id == document_id)
                .order_by(DocumentAccessKey.access_key)
            )
        ).all()
    )


async def filter_document_ids(
    db: AsyncSession, document_ids: Sequence[int], access_key: str | None
) -> list[int]:
    if access_key is None:
        return list(document_ids)
    if not document_ids:
        return []
    return list(
        (
            await db.scalars(
                select(Document.id).where(
                    Document.id.in_(document_ids),
                    Document.is_deleted.is_(False),
                    document_has_access_key(access_key),
                )
            )
        ).all()
    )


async def replace_document_access_keys(
    db: AsyncSession, document: Document, values: Sequence[str] | None
) -> list[str]:
    keys = normalize_access_keys(values)
    await db.execute(
        delete(DocumentAccessKey).where(DocumentAccessKey.document_id == document.id)
    )
    for key in keys:
        db.add(
            DocumentAccessKey(
                workspace_id=document.workspace_id,
                document_id=document.id,
                access_key=key,
            )
        )
    await db.flush()
    return keys
