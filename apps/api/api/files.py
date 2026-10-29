import logging
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..services.upload_settings import get_upload_limit_mb
from ..parsers.wps import WPS_MIMES
from ..core.db import get_db
from ..models.blobs import Blob
from ..models.documents import Document, DocumentSourceType, DocumentVersion
from ..models.processing import ProcessingJob
from ..storage import get_storage
from ..storage.base import BlobStorage, BlobTooLargeError
from .documents import build_document_response
from .schemas import BlobResponse, DocumentResponse

router = APIRouter(prefix="/files", tags=["files"])

logger = logging.getLogger(__name__)

_MIME_BY_EXTENSION = {
    ".png": {"image/png"},
    ".jpg": {"image/jpeg"},
    ".jpeg": {"image/jpeg"},
    ".webp": {"image/webp"},
    ".wps": set(WPS_MIMES) | {
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    },
    ".pdf": {"application/pdf"},
    ".doc": {
        "application/msword",
        "application/doc",
        "application/vnd.ms-word",
        "application/vnd.msword",
        "application/winword",
    },
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    },
    ".xlsx": {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    },
    ".xls": {
        "application/vnd.ms-excel",
        "application/excel",
        "application/x-excel",
        "application/x-msexcel",
    },
    ".md": {"text/markdown", "text/plain"},
    ".markdown": {"text/markdown", "text/plain"},
    ".txt": {"text/plain"},
}

# These formats may be advertised by browsers/WebDAV as their ZIP container.
# Only normalize known Office extensions; a MIME header does not prove validity.
# The format-specific worker parser validates the preserved original bytes.
_ZIP_MIMES = frozenset({
    "application/zip", "application/x-zip", "application/x-zip-compressed",
})
_OFFICE_CONTAINER_MIMES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".wps": "application/kswps",
}

# WPS Office family aliases. Each supported extension binds exactly to its own
# `application/wps-office.<extension>` declaration; matching returns the
# canonical MIME. Cross-extension aliases and unknown suffixes are rejected.
_WPS_OFFICE_ALIAS_MIMES = {
    ".doc": ("application/wps-office.doc", "application/msword"),
    ".docx": (
        "application/wps-office.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    ".wps": ("application/wps-office.wps", "application/kswps"),
    ".xls": ("application/wps-office.xls", "application/vnd.ms-excel"),
    ".xlsx": (
        "application/wps-office.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
}

# Diagnostic allowlists. Rejection logs may only name a value that the module
# already knows: an accepted extension, or a media type that appears in the
# tables above. Anything else is reported as "other" so a hostile or secret
# bearing header, filename or parameter never reaches the log.
_KNOWN_EXTENSIONS = frozenset(_MIME_BY_EXTENSION)
_KNOWN_MIMES = frozenset(
    mime
    for mimes in _MIME_BY_EXTENSION.values()
    for mime in mimes
) | frozenset(_ZIP_MIMES) | frozenset(_OFFICE_CONTAINER_MIMES.values()) | frozenset(
    alias for alias, _canonical in _WPS_OFFICE_ALIAS_MIMES.values()
) | {
    "application/msword",
    "text/plain",
    "application/pdf",
    "application/octet-stream",
}


def _safe_extension(extension: str) -> str:
    """Return the extension only when it is on the accepted whitelist."""
    return extension if extension in _KNOWN_EXTENSIONS else "other"


def _safe_mime(mime: str) -> str:
    """Return the media type only when the module already knows it."""
    return mime if mime in _KNOWN_MIMES else "other"


def normalized_filename(filename: str | None) -> str:
    safe_name = (filename or "未命名文件").replace("\\", "/").split("/")[-1]
    return safe_name[:1024] or "未命名文件"


def validate_file_type(filename: str, content_type: str | None) -> str:
    extension = Path(filename).suffix.lower()
    allowed_mimes = _MIME_BY_EXTENSION.get(extension)
    if allowed_mimes is None:
        logger.warning(
            "upload_type_rejected reason=unsupported_extension extension=%s",
            _safe_extension(extension),
        )
        raise HTTPException(
            status_code=415,
            detail="仅支持 PDF、Word、WPS 文字、Excel、Markdown、TXT 和 PNG/JPEG/WebP 图片",
        )
    normalized_mime = (content_type or "").split(";", 1)[0].strip().lower()
    normalized_mime = normalized_mime or "application/octet-stream"
    wps_alias = _WPS_OFFICE_ALIAS_MIMES.get(extension)
    if wps_alias is not None and normalized_mime == wps_alias[0]:
        return wps_alias[1]
    if normalized_mime in _ZIP_MIMES and extension in _OFFICE_CONTAINER_MIMES:
        return _OFFICE_CONTAINER_MIMES[extension]
    if (
        normalized_mime != "application/octet-stream"
        and normalized_mime not in allowed_mimes
    ):
        logger.warning(
            "upload_type_rejected reason=mime_mismatch extension=%s mime=%s",
            _safe_extension(extension),
            _safe_mime(normalized_mime),
        )
        raise HTTPException(
            status_code=415,
            detail="文件扩展名与内容类型不匹配",
        )
    if extension in {".png", ".jpg", ".jpeg", ".webp"}:
        return next(iter(allowed_mimes))
    return normalized_mime


@router.post("/upload", response_model=DocumentResponse, status_code=201)
async def upload_file(
    file: UploadFile = File(...),
    title: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    storage: BlobStorage = Depends(get_storage),
):
    filename = normalized_filename(file.filename)
    content_type = validate_file_type(filename, file.content_type)
    limit_mb = await get_upload_limit_mb(db)
    max_bytes = limit_mb * 1024 * 1024

    try:
        stored = storage.save(file.file, max_bytes=max_bytes)
    except BlobTooLargeError:
        raise HTTPException(
            status_code=413,
            detail=f"文件不能超过 {limit_mb} MiB",
        ) from None
    finally:
        await file.close()

    if stored.size == 0:
        storage.delete(stored.storage_key)
        raise HTTPException(status_code=400, detail="不能上传空文件")

    cleanup_storage_key: str | None = stored.storage_key
    try:
        existing_blob = await db.scalar(
            select(Blob).where(Blob.sha256 == stored.sha256)
        )
        if existing_blob is not None:
            storage.delete(stored.storage_key)
            cleanup_storage_key = None
            blob = existing_blob
        else:
            blob = Blob(
                sha256=stored.sha256,
                storage_key=stored.storage_key,
                content_type=content_type,
                file_size=stored.size,
                original_filename=filename,
            )
            db.add(blob)
            await db.flush()

        display_title = title.strip() or Path(filename).stem or filename
        document = Document(
            title=display_title[:1024],
            source_type=DocumentSourceType.file,
        )
        db.add(document)
        await db.flush()
        version = DocumentVersion(
            document_id=document.id,
            blob_id=blob.id,
            version_number=1,
            content_hash=stored.sha256,
            processing_status="created",
        )
        db.add(version)
        await db.flush()
        document.current_version_id = version.id
        db.add(
            ProcessingJob(
                document_id=document.id,
                document_version_id=version.id,
                stage="stored",
                status="created",
                idempotency_key=f"{version.id}:stored:v1",
                config_version="m1-v1",
            )
        )
        await db.commit()
        cleanup_storage_key = None
        await db.refresh(document)
        return await build_document_response(db, document)
    except Exception:
        await db.rollback()
        if cleanup_storage_key is not None:
            storage.delete(cleanup_storage_key)
        raise


@router.get("/blobs/{blob_id}", response_model=BlobResponse)
async def get_blob(
    blob_id: int,
    db: AsyncSession = Depends(get_db),
):
    blob = await db.get(Blob, blob_id)
    if blob is None:
        raise HTTPException(status_code=404, detail="原文件不存在")
    return BlobResponse.model_validate(blob)


@router.get("/blobs/{blob_id}/download")
async def download_blob(
    blob_id: int,
    db: AsyncSession = Depends(get_db),
    storage: BlobStorage = Depends(get_storage),
):
    blob = await db.get(Blob, blob_id)
    if blob is None:
        raise HTTPException(status_code=404, detail="原文件不存在")
    try:
        stream = storage.open(blob.storage_key)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="原文件内容已丢失") from None

    filename = quote(blob.original_filename or "download")
    return StreamingResponse(
        stream,
        media_type=blob.content_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            "Content-Length": str(blob.file_size),
        },
    )
