from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_db
from ..models.processing import ProcessingJob
from ..models.blobs import Blob
from ..models.documents import Document, DocumentVersion
from .schemas import (
    BlobResponse,
    DocumentResponse,
    DocumentVersionResponse,
    DocumentReprocessResponse,
    ProcessingJobResponse,
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
                structured_content=version.structured_content,
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


@router.post("/{document_id}/reprocess", response_model=DocumentReprocessResponse)
async def reprocess_document(
    document_id: int,
    db: AsyncSession = Depends(get_db),
):
    document = await db.get(Document, document_id)
    if document is None or document.is_deleted:
        raise HTTPException(status_code=404, detail="资料不存在")

    if document.current_version_id is None:
        raise HTTPException(status_code=400, detail="资料没有当前版本")

    version = await db.get(DocumentVersion, document.current_version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="当前版本不存在")

    idempotency_key = f"{document.id}:{document.current_version_id}:parsing:v1"
    existing_job = (await db.execute(
        select(ProcessingJob).where(ProcessingJob.idempotency_key == idempotency_key)
    )).scalar_one_or_none()

    if existing_job is not None:
        if existing_job.status in ("created", "retry"):
            return DocumentReprocessResponse(
                success=True,
                message="已在处理队列中",
                job_id=existing_job.id,
            )
        if existing_job.status == "processing":
            return DocumentReprocessResponse(
                success=True,
                message="正在处理中",
                job_id=existing_job.id,
            )
        existing_job.status = "created"
        existing_job.retry_count = 0
        existing_job.next_retry_at = None
        existing_job.last_error = None
        existing_job.error_details = None
        existing_job.started_at = None
        existing_job.finished_at = None
        job = existing_job
    else:
        job = ProcessingJob(
            document_id=document.id,
            document_version_id=version.id,
            stage="parsing",
            status="created",
            idempotency_key=idempotency_key,
            retry_count=0,
            max_retries=3,
            config_version="1",
        )

    version.processing_status = "created"
    db.add(job)
    db.add(version)
    await db.commit()
    await db.refresh(job)

    return DocumentReprocessResponse(
        success=True,
        message="已重新加入处理队列",
        job_id=job.id,
    )


@router.get("/{document_id}/latest-job", response_model=ProcessingJobResponse | None)
async def get_latest_job(
    document_id: int,
    db: AsyncSession = Depends(get_db),
):
    document = await db.get(Document, document_id)
    if document is None or document.is_deleted:
        raise HTTPException(status_code=404, detail="资料不存在")

    result = await db.execute(
        select(ProcessingJob)
        .where(ProcessingJob.document_id == document_id)
        .order_by(ProcessingJob.created_at.desc(), ProcessingJob.id.desc())
        .limit(1)
    )
    job = result.scalar_one_or_none()
    if job is None:
        return None

    return ProcessingJobResponse.model_validate(job)
