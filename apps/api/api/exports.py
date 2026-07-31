from __future__ import annotations

import json
from shutil import copyfileobj
from datetime import datetime, timezone
from tempfile import SpooledTemporaryFile
from typing import BinaryIO, Iterator
from urllib.parse import quote
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_db
from ..models.blobs import Blob
from ..models.documents import Document, DocumentVersion
from ..services.exports import (
    document_json_bytes,
    document_markdown_bytes,
    safe_export_stem,
    safe_original_filename,
)
from ..storage import get_storage
from ..storage.base import BlobStorage
from .documents import build_document_response

router = APIRouter(prefix="/exports", tags=["exports"])


def _attachment_response(
    content: bytes,
    *,
    filename: str,
    media_type: str,
) -> Response:
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(filename)}"
            )
        },
    )


async def _document_or_404(
    db: AsyncSession,
    document_id: int,
) -> Document:
    document = await db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="资料不存在")
    return document


@router.get("/documents/{document_id}/markdown")
async def export_document_markdown(
    document_id: int,
    db: AsyncSession = Depends(get_db),
):
    document = await _document_or_404(db, document_id)
    response = await build_document_response(db, document)
    return _attachment_response(
        document_markdown_bytes(response),
        filename=f"{safe_export_stem(response)}.md",
        media_type="text/markdown; charset=utf-8",
    )


@router.get("/documents/{document_id}/json")
async def export_document_json(
    document_id: int,
    db: AsyncSession = Depends(get_db),
):
    document = await _document_or_404(db, document_id)
    response = await build_document_response(db, document)
    return _attachment_response(
        document_json_bytes(response),
        filename=f"{safe_export_stem(response)}.json",
        media_type="application/json; charset=utf-8",
    )


def _stream_and_close(file: BinaryIO) -> Iterator[bytes]:
    try:
        while chunk := file.read(1024 * 1024):
            yield chunk
    finally:
        file.close()


@router.get("/library")
async def export_library(
    include_trashed: bool = Query(False),
    include_originals: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    storage: BlobStorage = Depends(get_storage),
):
    statement = select(Document).order_by(Document.id)
    if not include_trashed:
        statement = statement.where(Document.is_deleted.is_(False))
    documents = list(
        (await db.execute(statement))
        .scalars()
        .all()
    )
    archive = SpooledTemporaryFile(max_size=16 * 1024 * 1024, mode="w+b")
    exported_at = datetime.now(timezone.utc)
    manifest_documents: list[dict] = []
    with ZipFile(archive, mode="w", compression=ZIP_DEFLATED) as bundle:
        for document in documents:
            response = await build_document_response(db, document)
            stem = safe_export_stem(response)
            bundle.writestr(
                f"knowledge/{stem}.md",
                document_markdown_bytes(response),
            )
            bundle.writestr(
                f"data/{stem}.json",
                document_json_bytes(response),
            )
            original_path = None
            version = (
                await db.get(DocumentVersion, document.current_version_id)
                if document.current_version_id is not None
                else None
            )
            if include_originals and version is not None and version.blob_id:
                blob = await db.get(Blob, version.blob_id)
                if blob is not None and storage.exists(blob.storage_key):
                    original_name = safe_original_filename(
                        blob.original_filename,
                        blob.id,
                    )
                    original_path = f"originals/{stem}/{original_name}"
                    with storage.open(blob.storage_key) as source:
                        with bundle.open(original_path, mode="w") as target:
                            copyfileobj(source, target, length=1024 * 1024)
            manifest_documents.append(
                {
                    "id": response.id,
                    "title": response.title,
                    "is_deleted": response.is_deleted,
                    "markdown": f"knowledge/{stem}.md",
                    "json": f"data/{stem}.json",
                    "original": original_path,
                }
            )
        bundle.writestr(
            "manifest.json",
            (
                json.dumps(
                    {
                        "schema_version": "cangzhi.library.v1",
                        "exported_at": exported_at.isoformat(),
                        "document_count": len(manifest_documents),
                        "include_trashed": include_trashed,
                        "include_originals": include_originals,
                        "documents": manifest_documents,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            ).encode("utf-8"),
        )
        bundle.writestr(
            "README.txt",
            (
                "这是藏知知识库导出包。\n"
                "knowledge/ 是可直接阅读的 Markdown；data/ 是完整结构化 JSON；"
                "originals/（如有）保存原文件；manifest.json 是文件清单。\n"
            ).encode("utf-8"),
        )
    size = archive.tell()
    archive.seek(0)
    filename = f"cangzhi-export-{exported_at:%Y%m%d-%H%M%S}.zip"
    return StreamingResponse(
        _stream_and_close(archive),
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(filename)}"
            ),
            "Content-Length": str(size),
        },
    )
