from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.blobs import Blob
from ..models.chunks import DocumentChunk
from ..models.documents import Document, DocumentVersion
from ..models.embedding_profiles import ChunkEmbedding
from ..models.processing import ProcessingJob
from ..models.taxonomy import (
    DocumentCategory,
    DocumentSummary,
    DocumentTag,
)
from ..models.webdav import ExternalItemExclusion, WebDAVEntry
from ..storage.base import BlobStorage


@dataclass
class PurgeResult:
    affected: int = 0
    deleted_blob_count: int = 0
    blob_cleanup_errors: list[str] = field(default_factory=list)


async def trash_documents(
    db: AsyncSession,
    documents: list[Document],
    *,
    reason: str = "user",
    commit: bool = True,
) -> int:
    active = [document for document in documents if not document.is_deleted]
    if not active:
        return 0
    now = datetime.now(timezone.utc)
    ids = [document.id for document in active]
    entries = (
        (
            await db.execute(
                select(WebDAVEntry).where(WebDAVEntry.document_id.in_(ids))
            )
        )
        .scalars()
        .all()
    )
    for document in active:
        document.is_deleted = True
        document.deleted_at = now
        document.delete_reason = reason
    for entry in entries:
        if entry.state != "ignored":
            entry.resume_state = entry.state
        entry.state = "ignored"
        entry.ignore_reason = "document_trashed"
        entry.ignored_at = now
    if commit:
        await db.commit()
    return len(active)


async def restore_documents(
    db: AsyncSession,
    documents: list[Document],
    *,
    commit: bool = True,
) -> int:
    trashed = [document for document in documents if document.is_deleted]
    if not trashed:
        return 0
    ids = [document.id for document in trashed]
    entries = (
        (
            await db.execute(
                select(WebDAVEntry).where(WebDAVEntry.document_id.in_(ids))
            )
        )
        .scalars()
        .all()
    )
    for document in trashed:
        document.is_deleted = False
        document.deleted_at = None
        document.delete_reason = None
    for entry in entries:
        if entry.ignore_reason != "document_trashed":
            continue
        entry.state = entry.resume_state or "discovered"
        entry.resume_state = None
        entry.ignore_reason = None
        entry.ignored_at = None
    if commit:
        await db.commit()
    return len(trashed)


async def purge_documents(
    db: AsyncSession,
    documents: list[Document],
    storage: BlobStorage,
) -> PurgeResult:
    trashed = [document for document in documents if document.is_deleted]
    result = PurgeResult()
    if not trashed:
        return result

    document_ids = [document.id for document in trashed]
    versions = (
        (
            await db.execute(
                select(DocumentVersion).where(
                    DocumentVersion.document_id.in_(document_ids)
                )
            )
        )
        .scalars()
        .all()
    )
    version_ids = [version.id for version in versions]
    blob_ids = {version.blob_id for version in versions if version.blob_id is not None}
    entries = (
        (
            await db.execute(
                select(WebDAVEntry).where(WebDAVEntry.document_id.in_(document_ids))
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(timezone.utc)
    for entry in entries:
        if entry.external_identity:
            exclusion = await db.scalar(
                select(ExternalItemExclusion).where(
                    ExternalItemExclusion.external_identity
                    == entry.external_identity
                )
            )
            if exclusion is None:
                exclusion = ExternalItemExclusion(
                    source_type="webdav",
                    external_identity=entry.external_identity,
                    reason="permanent_deleted",
                    display_path=entry.remote_path,
                    source_snapshot={"source_id": entry.source_id},
                )
                db.add(exclusion)
            else:
                exclusion.reason = "permanent_deleted"
                exclusion.display_path = entry.remote_path
        entry.document_id = None
        entry.state = "ignored"
        entry.resume_state = None
        entry.ignore_reason = "permanent_deleted"
        entry.ignored_at = now

    for document in trashed:
        if document.external_identity:
            exclusion = await db.scalar(
                select(ExternalItemExclusion).where(
                    ExternalItemExclusion.external_identity
                    == document.external_identity
                )
            )
            if exclusion is None:
                meta = document.meta or {}
                db.add(
                    ExternalItemExclusion(
                        source_type=str(meta.get("external_source") or "webdav"),
                        external_identity=document.external_identity,
                        reason="permanent_deleted",
                        display_path=meta.get("webdav_path"),
                        source_snapshot={
                            "source_id": meta.get("webdav_source_id"),
                            "connector_name": meta.get("webdav_source_name"),
                        },
                    )
                )
        document.current_version_id = None
    await db.flush()

    if version_ids:
        chunk_ids = list(
            (
                await db.execute(
                    select(DocumentChunk.id).where(
                        DocumentChunk.document_version_id.in_(version_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        if chunk_ids:
            await db.execute(
                delete(ChunkEmbedding).where(ChunkEmbedding.chunk_id.in_(chunk_ids))
            )
        await db.execute(
            delete(DocumentCategory).where(
                DocumentCategory.document_version_id.in_(version_ids)
            )
        )
        await db.execute(
            delete(DocumentTag).where(
                DocumentTag.document_version_id.in_(version_ids)
            )
        )
        await db.execute(
            delete(DocumentSummary).where(
                DocumentSummary.document_version_id.in_(version_ids)
            )
        )
        await db.execute(
            delete(DocumentChunk).where(
                DocumentChunk.document_version_id.in_(version_ids)
            )
        )
        await db.execute(
            delete(ProcessingJob).where(
                ProcessingJob.document_version_id.in_(version_ids)
            )
        )
        await db.execute(
            delete(DocumentVersion).where(DocumentVersion.id.in_(version_ids))
        )
    await db.execute(delete(Document).where(Document.id.in_(document_ids)))
    await db.commit()
    result.affected = len(trashed)

    for blob_id in blob_ids:
        reference_count = int(
            (
                await db.execute(
                    select(func.count(DocumentVersion.id)).where(
                        DocumentVersion.blob_id == blob_id
                    )
                )
            ).scalar_one()
            or 0
        )
        if reference_count:
            continue
        blob = await db.get(Blob, blob_id)
        if blob is None:
            continue
        try:
            storage.delete(blob.storage_key)
        except Exception as exc:  # noqa: BLE001
            result.blob_cleanup_errors.append(
                f"Blob {blob.id} 清理失败：{type(exc).__name__}"
            )
            continue
        await db.delete(blob)
        await db.commit()
        result.deleted_blob_count += 1
    return result
