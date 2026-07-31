"""Canonical current-document reads shared by REST and MCP adapters."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.chunks import DocumentChunk
from ..models.documents import Document, DocumentVersion


class KnowledgeReadError(LookupError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


async def read_current_document(
    db: AsyncSession, document_id: int
) -> dict:
    row = (
        await db.execute(
            select(Document, DocumentVersion)
            .join(
                DocumentVersion,
                DocumentVersion.id == Document.current_version_id,
            )
            .where(
                Document.id == document_id,
                Document.is_deleted.is_(False),
            )
        )
    ).one_or_none()
    if row is None:
        raise KnowledgeReadError("document_not_found", "知识文档不存在")
    document, version = row
    return {
        "id": document.id,
        "title": document.title,
        "description": document.description,
        "source_type": document.source_type.value,
        "source_url": document.source_url,
        "updated_at": document.updated_at.isoformat(),
        "version": {
            "id": version.id,
            "number": version.version_number,
            "content_hash": version.content_hash,
            "processing_status": version.processing_status,
        },
        "content": version.raw_content,
        "structured_content": version.structured_content,
    }


async def read_current_chunk(db: AsyncSession, chunk_id: int) -> dict:
    chunk = (
        await db.execute(
            select(DocumentChunk)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                DocumentChunk.id == chunk_id,
                DocumentChunk.is_current.is_(True),
                Document.is_deleted.is_(False),
                Document.current_version_id == DocumentChunk.document_version_id,
            )
        )
    ).scalar_one_or_none()
    if chunk is None:
        raise KnowledgeReadError("chunk_not_found", "知识片段不存在")
    return chunk.to_dict()
