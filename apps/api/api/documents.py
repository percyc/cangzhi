from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_db
from ..models.blobs import Blob
from ..models.documents import Document, DocumentVersion
from .schemas import (
    BlobResponse,
    DocumentResponse,
    DocumentVersionResponse,
)

router = APIRouter(prefix="/documents", tags=["documents"])


async def build_document_response(
    db: AsyncSession,
    document: Document,
) -> DocumentResponse:
    version_response = None
    if document.current_version_id is not None:
        version = await db.get(DocumentVersion, document.current_version_id)
        if version is not None:
            blob_response = None
            if version.blob_id is not None:
                blob = await db.get(Blob, version.blob_id)
                if blob is not None:
                    blob_response = BlobResponse.model_validate(blob)
            version_response = DocumentVersionResponse(
                id=version.id,
                version_number=version.version_number,
                content_hash=version.content_hash,
                raw_content=version.raw_content,
                processing_status=version.processing_status,
                created_at=version.created_at,
                blob=blob_response,
            )

    return DocumentResponse(
        id=document.id,
        title=document.title,
        description=document.description,
        source_type=document.source_type.value,
        is_deleted=document.is_deleted,
        created_at=document.created_at,
        updated_at=document.updated_at,
        current_version=version_response,
    )


@router.get("", response_model=list[DocumentResponse])
async def list_documents(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Document)
        .where(Document.is_deleted.is_(False))
        .order_by(Document.updated_at.desc(), Document.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return [
        await build_document_response(db, document)
        for document in result.scalars().all()
    ]


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: int,
    db: AsyncSession = Depends(get_db),
):
    document = await db.get(Document, document_id)
    if document is None or document.is_deleted:
        raise HTTPException(status_code=404, detail="资料不存在")
    return await build_document_response(db, document)
