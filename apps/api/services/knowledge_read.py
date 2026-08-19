"""Canonical current-document reads shared by REST and MCP adapters."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.chunks import DocumentChunk
from ..models.documents import Document, DocumentVersion
from .scope_keys import DocumentSelection, candidate_condition


class KnowledgeReadError(LookupError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


async def list_current_documents(
    db: AsyncSession,
    *,
    limit: int = 50,
    offset: int = 0,
    document_selection: DocumentSelection | None = None,
    document_boundary: DocumentSelection | None = None,
) -> dict:
    """List active current documents without returning content or scope keys."""

    conditions = [
        Document.is_deleted.is_(False),
        Document.current_version_id == DocumentVersion.id,
    ]
    selection_condition = candidate_condition(document_selection)
    if selection_condition is not None:
        conditions.append(selection_condition)
    boundary_condition = candidate_condition(document_boundary)
    if boundary_condition is not None:
        conditions.append(boundary_condition)
    total = int(
        await db.scalar(
            select(func.count(Document.id))
            .select_from(Document)
            .join(DocumentVersion, DocumentVersion.id == Document.current_version_id)
            .where(*conditions)
        )
        or 0
    )
    rows = (
        await db.execute(
            select(Document, DocumentVersion)
            .join(DocumentVersion, DocumentVersion.id == Document.current_version_id)
            .where(*conditions)
            .order_by(Document.updated_at.desc(), Document.id.desc())
            .offset(max(0, offset))
            .limit(max(1, min(limit, 200)))
        )
    ).all()
    return {
        "items": [
            {
                "id": document.id,
                "title": document.title,
                "description": document.description,
                "source_type": document.source_type.value,
                "source_url": document.source_url,
                "external_id": document.external_identity,
                "updated_at": document.updated_at.isoformat(),
                "version": {
                    "id": version.id,
                    "number": version.version_number,
                    "processing_status": version.processing_status,
                },
            }
            for document, version in rows
        ],
        "total": total,
        "limit": max(1, min(limit, 200)),
        "offset": max(0, offset),
    }


async def read_current_document(
    db: AsyncSession,
    document_id: int,
    *,
    document_boundary: DocumentSelection | None = None,
) -> dict:
    statement = (
        select(Document, DocumentVersion)
        .join(DocumentVersion, DocumentVersion.id == Document.current_version_id)
        .where(Document.id == document_id, Document.is_deleted.is_(False))
    )
    boundary_condition = candidate_condition(document_boundary)
    if boundary_condition is not None:
        statement = statement.where(boundary_condition)
    row = (
        await db.execute(statement)
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


async def read_current_chunk(
    db: AsyncSession,
    chunk_id: int,
    *,
    document_boundary: DocumentSelection | None = None,
) -> dict:
    statement = (
        select(DocumentChunk)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(
            DocumentChunk.id == chunk_id,
            DocumentChunk.is_current.is_(True),
            Document.is_deleted.is_(False),
            Document.current_version_id == DocumentChunk.document_version_id,
        )
    )
    boundary_condition = candidate_condition(document_boundary)
    if boundary_condition is not None:
        statement = statement.where(boundary_condition)
    chunk = (
        await db.execute(statement)
    ).scalar_one_or_none()
    if chunk is None:
        raise KnowledgeReadError("chunk_not_found", "知识片段不存在")
    return chunk.to_dict()
