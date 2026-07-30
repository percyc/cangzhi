from io import BytesIO
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.db import get_db
from ..models.auth import AIRuntimeConfig
from ..models.blobs import Blob
from ..models.chunks import DocumentChunk
from ..models.documents import Document, DocumentSourceType, DocumentVersion
from ..models.embedding_profiles import ChunkEmbedding, EmbeddingProfile
from ..models.processing import ProcessingJob
from ..models.taxonomy import (
    Category,
    DocumentCategory,
    DocumentSummary,
    DocumentTag,
    Tag,
)
from ..models.webdav import WebDAVEntry, WebDAVSource
from ..security.secrets import decrypt_secret
from ..security.url_safety import URLSecurityError
from ..services.webdav import WebDAVError, download_file
from ..services.document_lifecycle import (
    purge_documents as purge_document_records,
    restore_documents as restore_document_records,
    trash_documents as trash_document_records,
)
from ..storage import get_storage
from ..storage.base import BlobStorage
from .schemas import (
    BlobResponse,
    CategoryMini,
    DocumentBatchActionRequest,
    DocumentBatchOrganizeRequest,
    DocumentCategoryUpdateRequest,
    DocumentMetadataUpdateRequest,
    DocumentReprocessResponse,
    DocumentResponse,
    DocumentSummaryResponse,
    DocumentTagsUpdateRequest,
    DocumentVersionResponse,
    ProcessingJobResponse,
    TagMini,
)

router = APIRouter(prefix="/documents", tags=["documents"])

_RUNNING_JOB_STATUSES = {"created", "processing", "retry"}


def _download_response(
    stream,
    *,
    filename: str,
    content_type: str,
    content_length: int,
) -> StreamingResponse:
    encoded_filename = quote(filename or "download")
    return StreamingResponse(
        stream,
        media_type=content_type or "application/octet-stream",
        headers={
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{encoded_filename}"
            ),
            "Content-Length": str(content_length),
        },
    )


def _stage_payload(
    job: ProcessingJob | None, *, skipped_reason: str | None = None
) -> dict:
    if job is None:
        return {
            "status": "skipped" if skipped_reason else "pending",
            "message": skipped_reason or "等待前一阶段完成",
            "last_error": None,
        }
    labels = {
        "created": "等待处理",
        "processing": "正在处理",
        "retry": "等待重试",
        "completed": "已完成",
        "failed": "处理失败",
    }
    return {
        "status": job.status,
        "message": labels.get(job.status, job.status),
        "last_error": job.last_error,
    }


async def _load_categories_for_versions(
    db: AsyncSession,
    version_ids: list[int],
) -> dict[int, tuple[CategoryMini | None, list[CategoryMini]]]:
    if not version_ids:
        return {}
    result = await db.execute(
        select(DocumentCategory, Category)
        .join(Category, Category.id == DocumentCategory.category_id)
        .where(DocumentCategory.document_version_id.in_(version_ids))
        .order_by(DocumentCategory.id)
    )
    grouped: dict[int, list[tuple[DocumentCategory, Category]]] = {}
    for link, category in result.all():
        grouped.setdefault(link.document_version_id, []).append((link, category))
    output: dict[int, tuple[CategoryMini | None, list[CategoryMini]]] = {}
    for version_id, links in grouped.items():
        cats = [
            CategoryMini.model_validate(cat)
            for _link, cat in sorted(links, key=lambda item: item[0].id)
        ]
        primary = next(
            (cat for (link, cat) in links if link.is_primary),
            None,
        )
        primary_dto = (
            CategoryMini.model_validate(primary) if primary is not None else None
        )
        output[version_id] = (primary_dto, cats)
    return output


async def _load_tags_for_versions(
    db: AsyncSession,
    version_ids: list[int],
) -> dict[int, list[TagMini]]:
    if not version_ids:
        return {}
    result = await db.execute(
        select(DocumentTag, Tag)
        .join(Tag, Tag.id == DocumentTag.tag_id)
        .where(DocumentTag.document_version_id.in_(version_ids))
        .order_by(DocumentTag.id)
    )
    grouped: dict[int, list[TagMini]] = {}
    for _link, tag in result.all():
        grouped.setdefault(_link.document_version_id, []).append(
            TagMini.model_validate(tag)
        )
    return grouped


async def _load_summaries_for_versions(
    db: AsyncSession,
    version_ids: list[int],
) -> dict[int, DocumentSummaryResponse]:
    if not version_ids:
        return {}
    result = await db.execute(
        select(DocumentSummary).where(
            DocumentSummary.document_version_id.in_(version_ids)
        )
    )
    return {
        summary.document_version_id: DocumentSummaryResponse.model_validate(summary)
        for summary in result.scalars().all()
    }


async def build_document_response(
    db: AsyncSession,
    document: Document,
) -> DocumentResponse:
    version_response: DocumentVersionResponse | None = None
    version_id: int | None = None
    if document.current_version_id is not None:
        version = await db.get(DocumentVersion, document.current_version_id)
        if version is not None:
            version_id = version.id
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
                meta=version.meta or {},
                created_at=version.created_at,
                blob=blob_response,
                source_url=document.source_url,
            )

    primary_category = None
    categories: list[CategoryMini] = []
    tags: list[TagMini] = []
    summary: DocumentSummaryResponse | None = None
    if version_id is not None:
        cat_map = await _load_categories_for_versions(db, [version_id])
        tag_map = await _load_tags_for_versions(db, [version_id])
        summary_map = await _load_summaries_for_versions(db, [version_id])
        primary_category, categories = cat_map.get(version_id, (None, []))
        tags = tag_map.get(version_id, [])
        summary = summary_map.get(version_id)

    origin = None
    document_meta = document.meta or {}
    if document_meta.get("external_source") == "webdav":
        connector_id = document_meta.get("webdav_source_id")
        connector = (
            await db.get(WebDAVSource, connector_id)
            if isinstance(connector_id, int)
            else None
        )
        source_entry = await db.scalar(
            select(WebDAVEntry).where(WebDAVEntry.document_id == document.id)
        )
        origin = {
            "kind": "webdav",
            "label": (
                connector.name
                if connector
                else document_meta.get("webdav_source_name")
                or "已删除的 WebDAV 连接器"
            ),
            "connector_id": connector_id,
            "remote_path": document_meta.get("webdav_path"),
            "connector_available": connector is not None,
            "source_status": source_entry.state if source_entry else None,
        }

    return DocumentResponse(
        id=document.id,
        title=document.title,
        description=document.description,
        source_type=document.source_type.value,
        source_url=document.source_url,
        origin=origin,
        is_deleted=document.is_deleted,
        deleted_at=document.deleted_at,
        delete_reason=document.delete_reason,
        created_at=document.created_at,
        updated_at=document.updated_at,
        current_version=version_response,
        primary_category=primary_category,
        categories=categories,
        tags=tags,
        summary=summary,
    )


@router.get("", response_model=list[DocumentResponse])
async def list_documents(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    deleted: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Document)
        .where(Document.is_deleted.is_(deleted))
        .order_by(Document.updated_at.desc(), Document.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return [
        await build_document_response(db, document)
        for document in result.scalars().all()
    ]


@router.post("/batch/trash", response_model=dict)
async def trash_documents(
    payload: DocumentBatchActionRequest,
    db: AsyncSession = Depends(get_db),
):
    documents = (
        (
            await db.execute(
                select(Document).where(Document.id.in_(payload.document_ids))
            )
        )
        .scalars()
        .all()
    )
    affected = await trash_document_records(db, list(documents))
    return {"ok": True, "affected": affected}


@router.post("/batch/restore", response_model=dict)
async def restore_documents(
    payload: DocumentBatchActionRequest,
    db: AsyncSession = Depends(get_db),
):
    documents = (
        (
            await db.execute(
                select(Document).where(Document.id.in_(payload.document_ids))
            )
        )
        .scalars()
        .all()
    )
    affected = await restore_document_records(db, list(documents))
    return {"ok": True, "affected": affected}


@router.post("/batch/permanent-delete", response_model=dict)
async def permanently_delete_documents(
    payload: DocumentBatchActionRequest,
    db: AsyncSession = Depends(get_db),
    storage: BlobStorage = Depends(get_storage),
):
    documents = list(
        (
            await db.execute(
                select(Document).where(
                    Document.id.in_(payload.document_ids),
                    Document.is_deleted.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    result = await purge_document_records(db, documents, storage)
    return {
        "ok": True,
        "affected": result.affected,
        "deleted_blobs": result.deleted_blob_count,
        "cleanup_warnings": result.blob_cleanup_errors,
    }


@router.delete("/trash/empty", response_model=dict)
async def empty_trash(
    db: AsyncSession = Depends(get_db),
    storage: BlobStorage = Depends(get_storage),
):
    documents = list(
        (
            await db.execute(
                select(Document).where(Document.is_deleted.is_(True))
            )
        )
        .scalars()
        .all()
    )
    result = await purge_document_records(db, documents, storage)
    return {
        "ok": True,
        "affected": result.affected,
        "deleted_blobs": result.deleted_blob_count,
        "cleanup_warnings": result.blob_cleanup_errors,
    }


@router.post("/batch/organize", response_model=dict)
async def organize_documents(
    payload: DocumentBatchOrganizeRequest,
    db: AsyncSession = Depends(get_db),
):
    documents = (
        (
            await db.execute(
                select(Document).where(
                    Document.id.in_(payload.document_ids),
                    Document.is_deleted.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    category = (
        await db.get(Category, payload.category_id)
        if payload.category_id is not None
        else None
    )
    if payload.category_id is not None and category is None:
        raise HTTPException(status_code=404, detail="分类不存在")
    tags = (
        (
            await db.execute(select(Tag).where(Tag.id.in_(payload.add_tag_ids)))
        )
        .scalars()
        .all()
        if payload.add_tag_ids
        else []
    )
    if len(tags) != len(set(payload.add_tag_ids)):
        raise HTTPException(status_code=404, detail="部分标签不存在")

    affected = 0
    for document in documents:
        if document.current_version_id is None:
            continue
        if category is not None:
            await db.execute(
                delete(DocumentCategory).where(
                    DocumentCategory.document_version_id
                    == document.current_version_id
                )
            )
            db.add(
                DocumentCategory(
                    document_id=document.id,
                    document_version_id=document.current_version_id,
                    category_id=category.id,
                    is_primary=True,
                    confidence=1.0,
                    source="user",
                )
            )
        if tags:
            existing_tag_ids = set(
                (
                    await db.execute(
                        select(DocumentTag.tag_id).where(
                            DocumentTag.document_version_id
                            == document.current_version_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            for tag in tags:
                if tag.id not in existing_tag_ids:
                    db.add(
                        DocumentTag(
                            document_id=document.id,
                            document_version_id=document.current_version_id,
                            tag_id=tag.id,
                            confidence=1.0,
                            source="user",
                        )
                    )
        affected += 1
    await db.commit()
    return {"ok": True, "affected": affected}


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: int,
    db: AsyncSession = Depends(get_db),
):
    document = await db.get(Document, document_id)
    if document is None or document.is_deleted:
        raise HTTPException(status_code=404, detail="资料不存在")
    return await build_document_response(db, document)


@router.patch("/{document_id}/metadata", response_model=DocumentResponse)
async def update_document_metadata(
    document_id: int,
    payload: DocumentMetadataUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    document = await db.get(Document, document_id)
    if document is None or document.is_deleted:
        raise HTTPException(status_code=404, detail="资料不存在")
    if payload.title is not None:
        document.title = payload.title.strip()
    if "description" in payload.model_fields_set:
        document.description = payload.description
    if "summary" in payload.model_fields_set:
        if document.current_version_id is None:
            raise HTTPException(status_code=400, detail="资料没有当前版本")
        summary = (
            await db.execute(
                select(DocumentSummary).where(
                    DocumentSummary.document_version_id
                    == document.current_version_id
                )
            )
        ).scalar_one_or_none()
        value = (payload.summary or "").strip()
        if not value and summary is not None:
            await db.delete(summary)
        elif value:
            if summary is None:
                summary = DocumentSummary(
                    document_id=document.id,
                    document_version_id=document.current_version_id,
                    summary=value,
                    source="user",
                )
                db.add(summary)
            else:
                summary.summary = value
                summary.source = "user"
                summary.confidence = 1.0
    await db.commit()
    await db.refresh(document)
    return await build_document_response(db, document)


@router.patch("/{document_id}/tags", response_model=DocumentResponse)
async def update_document_tags(
    document_id: int,
    payload: DocumentTagsUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    document = await db.get(Document, document_id)
    if document is None or document.is_deleted:
        raise HTTPException(status_code=404, detail="资料不存在")
    if document.current_version_id is None:
        raise HTTPException(status_code=400, detail="资料没有当前版本")
    tags = (
        (
            await db.execute(select(Tag).where(Tag.id.in_(payload.tag_ids)))
        )
        .scalars()
        .all()
        if payload.tag_ids
        else []
    )
    if len(tags) != len(set(payload.tag_ids)):
        raise HTTPException(status_code=404, detail="部分标签不存在")
    await db.execute(
        delete(DocumentTag).where(
            DocumentTag.document_version_id == document.current_version_id
        )
    )
    for tag in tags:
        db.add(
            DocumentTag(
                document_id=document.id,
                document_version_id=document.current_version_id,
                tag_id=tag.id,
                confidence=1.0,
                source="user",
            )
        )
    await db.commit()
    await db.refresh(document)
    return await build_document_response(db, document)


@router.delete("/{document_id}", response_model=dict)
async def trash_document(
    document_id: int,
    db: AsyncSession = Depends(get_db),
):
    document = await db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="资料不存在")
    await trash_document_records(db, [document])
    return {"ok": True, "message": "资料已移入回收站"}


@router.post("/{document_id}/restore", response_model=dict)
async def restore_document(
    document_id: int,
    db: AsyncSession = Depends(get_db),
):
    document = await db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="资料不存在")
    await restore_document_records(db, [document])
    return {"ok": True, "message": "资料已恢复"}


@router.delete("/{document_id}/permanent", response_model=dict)
async def permanently_delete_document(
    document_id: int,
    db: AsyncSession = Depends(get_db),
    storage: BlobStorage = Depends(get_storage),
):
    document = await db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="资料不存在")
    if not document.is_deleted:
        raise HTTPException(status_code=409, detail="请先将资料移入回收站")
    result = await purge_document_records(db, [document], storage)
    return {
        "ok": True,
        "message": "资料已永久删除",
        "deleted_blobs": result.deleted_blob_count,
        "cleanup_warnings": result.blob_cleanup_errors,
    }


@router.get("/{document_id}/original")
async def download_document_original(
    document_id: int,
    db: AsyncSession = Depends(get_db),
    storage: BlobStorage = Depends(get_storage),
):
    """Download an uploaded original or fetch a WebDAV original on demand."""

    document = await db.get(Document, document_id)
    if document is None or document.is_deleted:
        raise HTTPException(status_code=404, detail="资料不存在")
    version = (
        await db.get(DocumentVersion, document.current_version_id)
        if document.current_version_id is not None
        else None
    )
    if version is None:
        raise HTTPException(status_code=404, detail="资料没有当前版本")

    document_meta = document.meta or {}
    if document_meta.get("external_source") == "webdav":
        source_id = document_meta.get("webdav_source_id")
        source = (
            await db.get(WebDAVSource, source_id)
            if isinstance(source_id, int)
            else None
        )
        entry = (
            await db.scalar(
                select(WebDAVEntry).where(
                    WebDAVEntry.document_id == document.id,
                    WebDAVEntry.source_id == source_id,
                )
            )
            if source is not None
            else None
        )
        if source is None or entry is None:
            raise HTTPException(
                status_code=409,
                detail="原 WebDAV 连接器已不存在，无法获取原文件",
            )
        try:
            payload, upstream_type = await download_file(
                base_url=source.base_url,
                remote_path=entry.remote_path,
                username=source.username,
                password=decrypt_secret(source.password_cipher),
                trusted_private_network=source.trusted_private_network,
                max_bytes=settings.max_upload_size_mb * 1024 * 1024,
            )
        except (WebDAVError, URLSecurityError) as exc:
            raise HTTPException(
                status_code=502,
                detail=f"获取 WebDAV 原文件失败：{exc}",
            ) from None
        return _download_response(
            BytesIO(payload),
            filename=entry.remote_path.rsplit("/", 1)[-1],
            content_type=upstream_type.split(";", 1)[0].strip(),
            content_length=len(payload),
        )

    if version.blob_id is None:
        raise HTTPException(status_code=404, detail="原文件不存在")
    blob = await db.get(Blob, version.blob_id)
    if blob is None:
        raise HTTPException(status_code=404, detail="原文件不存在")
    try:
        stream = storage.open(blob.storage_key)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="原文件内容已丢失") from None
    return _download_response(
        stream,
        filename=blob.original_filename or "download",
        content_type=blob.content_type,
        content_length=blob.file_size,
    )


@router.post("/{document_id}/reprocess", response_model=DocumentReprocessResponse)
async def reprocess_document(
    document_id: int,
    db: AsyncSession = Depends(get_db),
    storage: BlobStorage = Depends(get_storage),
):
    document = await db.get(Document, document_id)
    if document is None or document.is_deleted:
        raise HTTPException(status_code=404, detail="资料不存在")

    if document.current_version_id is None:
        raise HTTPException(status_code=400, detail="资料没有当前版本")
    version = await db.get(DocumentVersion, document.current_version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="当前版本不存在")

    document_meta = document.meta or {}
    if (
        document_meta.get("external_source") == "webdav"
        and version.blob_id is None
    ):
        source_id = document_meta.get("webdav_source_id")
        source = (
            await db.get(WebDAVSource, source_id)
            if isinstance(source_id, int)
            else None
        )
        entry = (
            await db.scalar(
                select(WebDAVEntry).where(
                    WebDAVEntry.document_id == document.id,
                    WebDAVEntry.source_id == source_id,
                )
            )
            if source is not None
            else None
        )
        if source is None or entry is None:
            raise HTTPException(
                status_code=409,
                detail="原 WebDAV 连接器已不存在，无法重新获取原文件",
            )
        try:
            payload, upstream_type = await download_file(
                base_url=source.base_url,
                remote_path=entry.remote_path,
                username=source.username,
                password=decrypt_secret(source.password_cipher),
                trusted_private_network=source.trusted_private_network,
                max_bytes=settings.max_upload_size_mb * 1024 * 1024,
            )
        except (WebDAVError, URLSecurityError) as exc:
            raise HTTPException(
                status_code=502,
                detail=f"重新获取 WebDAV 原文件失败：{exc}",
            ) from None
        stored = storage.save(
            BytesIO(payload),
            max_bytes=settings.max_upload_size_mb * 1024 * 1024,
        )
        blob = await db.scalar(select(Blob).where(Blob.sha256 == stored.sha256))
        if blob is None:
            blob = Blob(
                sha256=stored.sha256,
                storage_key=stored.storage_key,
                content_type=upstream_type.split(";", 1)[0],
                file_size=stored.size,
                original_filename=entry.remote_path.rsplit("/", 1)[-1],
            )
            db.add(blob)
            await db.flush()
        else:
            storage.delete(stored.storage_key)
        version.blob_id = blob.id

    idempotency_key = f"{document.id}:{document.current_version_id}:parsing:v1"
    existing_job = (
        await db.execute(
            select(ProcessingJob).where(
                ProcessingJob.idempotency_key == idempotency_key
            )
        )
    ).scalar_one_or_none()

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
            config_version="2",
        )

    version.processing_status = "created"
    if document.source_type == DocumentSourceType.url:
        # A manual reprocess of a URL means "fetch the page again".
        # Reusing the old snapshot would preserve a challenge/error
        # page forever after browser headers or site behaviour change.
        version.blob_id = None
        version.content_hash = ""
    db.add(job)
    db.add(version)
    await db.commit()
    await db.refresh(job)

    return DocumentReprocessResponse(
        success=True,
        message="已重新加入处理队列",
        job_id=job.id,
    )


@router.patch("/{document_id}/category", response_model=DocumentResponse)
async def update_document_category(
    document_id: int,
    payload: DocumentCategoryUpdateRequest,
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

    category = await db.get(Category, payload.category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="分类不存在")

    existing = (
        (
            await db.execute(
                select(DocumentCategory).where(
                    DocumentCategory.document_version_id == version.id
                )
            )
        )
        .scalars()
        .all()
    )
    for link in existing:
        await db.delete(link)
    await db.flush()

    db.add(
        DocumentCategory(
            document_id=document.id,
            document_version_id=version.id,
            category_id=category.id,
            is_primary=True,
            confidence=1.0,
            source="user",
        )
    )
    await db.commit()
    await db.refresh(document)
    return await build_document_response(db, document)


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


@router.get("/{document_id}/processing-status", response_model=dict)
async def get_processing_status(
    document_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Return the full ingestion pipeline status for the current version."""

    document = await db.get(Document, document_id)
    if document is None or document.is_deleted:
        raise HTTPException(status_code=404, detail="资料不存在")
    if document.current_version_id is None:
        raise HTTPException(status_code=400, detail="资料没有当前版本")
    version = await db.get(DocumentVersion, document.current_version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="当前版本不存在")

    jobs = (
        (
            await db.execute(
                select(ProcessingJob)
                .where(
                    ProcessingJob.document_id == document_id,
                    ProcessingJob.document_version_id == document.current_version_id,
                    ProcessingJob.stage.in_(("parsing", "chunking", "understanding")),
                )
                .order_by(ProcessingJob.id.desc())
            )
        )
        .scalars()
        .all()
    )
    latest_by_stage: dict[str, ProcessingJob] = {}
    for job in jobs:
        latest_by_stage.setdefault(job.stage, job)

    chunk_count = int(
        (
            await db.execute(
                select(func.count(DocumentChunk.id)).where(
                    DocumentChunk.document_version_id == document.current_version_id,
                    DocumentChunk.role == "child",
                    DocumentChunk.is_current.is_(True),
                )
            )
        ).scalar_one()
        or 0
    )
    config = (
        (
            await db.execute(
                select(AIRuntimeConfig).order_by(AIRuntimeConfig.id.desc()).limit(1)
            )
        )
        .scalars()
        .first()
    )
    active_profile: EmbeddingProfile | None = None
    if config is not None and config.active_embedding_profile_id is not None:
        active_profile = await db.get(
            EmbeddingProfile, config.active_embedding_profile_id
        )

    embedded_count = 0
    failed_embedding_jobs = 0
    running_embedding_jobs = 0
    if active_profile is not None:
        embedded_count = int(
            (
                await db.execute(
                    select(func.count(ChunkEmbedding.id))
                    .join(
                        DocumentChunk,
                        DocumentChunk.id == ChunkEmbedding.chunk_id,
                    )
                    .where(
                        ChunkEmbedding.profile_id == active_profile.id,
                        DocumentChunk.document_version_id
                        == document.current_version_id,
                        DocumentChunk.role == "child",
                        DocumentChunk.is_current.is_(True),
                    )
                )
            ).scalar_one()
            or 0
        )
        embedding_status_rows = (
            await db.execute(
                select(ProcessingJob.status, func.count(ProcessingJob.id))
                .where(
                    ProcessingJob.document_version_id == document.current_version_id,
                    ProcessingJob.stage == "embedding",
                    ProcessingJob.embedding_profile_id == active_profile.id,
                )
                .group_by(ProcessingJob.status)
            )
        ).all()
        status_counts = {status: int(count) for status, count in embedding_status_rows}
        failed_embedding_jobs = status_counts.get("failed", 0)
        running_embedding_jobs = sum(
            status_counts.get(status, 0) for status in _RUNNING_JOB_STATUSES
        )

    parsing = _stage_payload(latest_by_stage.get("parsing"))
    if (
        latest_by_stage.get("parsing") is None
        and version.processing_status in {"ready", "unsupported"}
        and (version.raw_content or version.structured_content)
    ):
        parsing = {
            "status": "completed",
            "message": "已提取正文",
            "last_error": None,
        }

    chunking = _stage_payload(latest_by_stage.get("chunking"))
    if chunk_count > 0:
        chunking = {
            "status": "completed",
            "message": "已生成知识切片",
            "last_error": None,
        }

    understanding = _stage_payload(latest_by_stage.get("understanding"))
    ai_status = (version.meta or {}).get("ai_status")
    if ai_status in {"completed", "not_configured"}:
        understanding = {
            "status": "completed" if ai_status == "completed" else "skipped",
            "message": (
                "AI 整理已完成"
                if ai_status == "completed"
                else "未配置对话模型，已按默认规则整理"
            ),
            "last_error": None,
        }
    elif latest_by_stage.get("understanding") is None and chunk_count > 0:
        understanding = {
            "status": "skipped",
            "message": "历史资料无 AI 整理任务记录",
            "last_error": None,
        }
    if active_profile is None:
        embedding = {
            "status": "disabled",
            "message": "未启用向量索引，仍可使用关键词检索",
            "profile_id": None,
            "model": None,
            "completed": 0,
            "total": chunk_count,
            "failed": 0,
        }
    elif failed_embedding_jobs:
        embedding_status = "failed"
        embedding_message = f"{failed_embedding_jobs} 个向量任务失败"
        embedding = {
            "status": embedding_status,
            "message": embedding_message,
            "profile_id": active_profile.id,
            "model": active_profile.model,
            "completed": embedded_count,
            "total": chunk_count,
            "failed": failed_embedding_jobs,
        }
    elif chunk_count > 0 and embedded_count >= chunk_count:
        embedding = {
            "status": "completed",
            "message": "向量已完成，可进行语义检索",
            "profile_id": active_profile.id,
            "model": active_profile.model,
            "completed": embedded_count,
            "total": chunk_count,
            "failed": 0,
        }
    else:
        embedding = {
            "status": "processing" if running_embedding_jobs else "pending",
            "message": (
                "正在生成向量" if running_embedding_jobs else "等待切片完成后生成向量"
            ),
            "profile_id": active_profile.id,
            "model": active_profile.model,
            "completed": embedded_count,
            "total": chunk_count,
            "failed": 0,
        }

    stage_statuses = [
        parsing["status"],
        chunking["status"],
        understanding["status"],
        embedding["status"],
    ]
    if "failed" in stage_statuses:
        overall = "failed"
    elif any(
        status in _RUNNING_JOB_STATUSES or status == "pending"
        for status in stage_statuses
    ):
        overall = "processing"
    else:
        overall = "completed"

    return {
        "document_id": document.id,
        "document_version_id": document.current_version_id,
        "overall_status": overall,
        "keyword_searchable": chunk_count > 0,
        "vector_searchable": embedding["status"] == "completed",
        "stages": {
            "parsing": parsing,
            "understanding": understanding,
            "chunking": {
                **chunking,
                "child_chunks": chunk_count,
            },
            "embedding": embedding,
        },
    }
