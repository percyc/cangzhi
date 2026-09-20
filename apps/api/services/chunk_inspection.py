"""Read-only inspection of the chunks currently serving retrieval."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.chunks import DocumentChunk
from ..models.documents import Document, DocumentVersion


class ChunkInspectionError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


async def inspect_current_chunks(
    db: AsyncSession,
    document_id: int,
    *,
    offset: int,
    limit: int,
) -> dict:
    """Return a bounded page of online child chunks without model calls."""

    document = await db.scalar(
        select(Document).where(
            Document.id == document_id,
            Document.is_deleted.is_(False),
        )
    )
    if document is None:
        raise ChunkInspectionError(404, "资料不存在")
    if document.current_version_id is None:
        raise ChunkInspectionError(409, "资料还没有当前版本")
    version = await db.get(DocumentVersion, document.current_version_id)
    if version is None:
        raise ChunkInspectionError(409, "资料当前版本不存在")

    base = (
        DocumentChunk.document_version_id == version.id,
        DocumentChunk.is_current.is_(True),
    )
    child_total = int(
        await db.scalar(
            select(func.count())
            .select_from(DocumentChunk)
            .where(*base, DocumentChunk.role == "child")
        )
        or 0
    )
    parent_total = int(
        await db.scalar(
            select(func.count())
            .select_from(DocumentChunk)
            .where(*base, DocumentChunk.role == "parent")
        )
        or 0
    )
    chunks = list(
        (
            await db.scalars(
                select(DocumentChunk)
                .where(*base, DocumentChunk.role == "child")
                .order_by(DocumentChunk.order_index, DocumentChunk.id)
                .offset(offset)
                .limit(limit)
            )
        ).all()
    )
    items = []
    for chunk in chunks:
        content = chunk.content or ""
        items.append(
            {
                "id": chunk.id,
                "order_index": chunk.order_index,
                "chunk_type": chunk.chunk_type,
                "page": chunk.page,
                "heading_path": list(chunk.heading_path or []),
                "char_count": chunk.char_count,
                "content": content[:2_400],
                "truncated": len(content) > 2_400,
            }
        )
    quality = (version.meta or {}).get("chunk_quality")
    return {
        "document_id": document.id,
        "document_version_id": version.id,
        "child_total": child_total,
        "parent_total": parent_total,
        "offset": offset,
        "limit": limit,
        "quality": quality if isinstance(quality, dict) else None,
        "items": items,
    }
