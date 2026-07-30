from __future__ import annotations

from datetime import datetime, timezone
from fnmatch import fnmatchcase
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.db import get_db
from ..models.blobs import Blob
from ..models.documents import Document, DocumentSourceType, DocumentVersion
from ..models.processing import ProcessingJob
from ..models.webdav import WebDAVEntry, WebDAVSource
from ..security.secrets import decrypt_secret, encrypt_secret
from ..security.url_safety import URLSecurityError
from ..services.webdav import (
    WebDAVError,
    download_file,
    propfind,
    validate_webdav_url,
)
from ..storage import get_storage
from ..storage.base import BlobStorage
from .files import normalized_filename, validate_file_type

router = APIRouter(prefix="/webdav", tags=["webdav"])
DEFAULT_EXTENSIONS = [".pdf", ".doc", ".docx", ".md", ".markdown", ".txt"]


class SourceConfig(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    base_url: str = Field(min_length=1, max_length=1024)
    username: str = Field(default="", max_length=255)
    root_path: str = Field(default="/", max_length=2048)
    recursive: bool = True
    trusted_private_network: bool = False
    include_extensions: list[str] = Field(
        default_factory=lambda: DEFAULT_EXTENSIONS.copy()
    )
    ignore_patterns: list[str] = Field(
        default_factory=lambda: [".*", "~$*", "*.tmp", "@eaDir"]
    )

    @field_validator("include_extensions")
    @classmethod
    def normalize_extensions(cls, value: list[str]) -> list[str]:
        result = []
        for item in value:
            extension = item.strip().lower()
            if extension and not extension.startswith("."):
                extension = "." + extension
            if extension and extension not in result:
                result.append(extension)
        if not result:
            raise ValueError("至少需要配置一种纳入的文件类型")
        return result

    @field_validator("ignore_patterns")
    @classmethod
    def normalize_ignore_patterns(cls, value: list[str]) -> list[str]:
        result = []
        for item in value:
            pattern = item.strip()
            if pattern and pattern not in result:
                result.append(pattern)
        return result


class SourceCreate(SourceConfig):
    password: str = Field(default="", max_length=1024)


class SourceUpdate(SourceConfig):
    password: str = Field(default="", max_length=1024)
    clear_password: bool = False


async def _source_or_404(db: AsyncSession, source_id: int) -> WebDAVSource:
    source = await db.get(WebDAVSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="WebDAV 知识源不存在")
    return source


@router.get("", response_model=list[dict[str, Any]])
async def list_sources(db: AsyncSession = Depends(get_db)):
    rows = list(
        (
            await db.execute(
                select(WebDAVSource).order_by(WebDAVSource.id.desc())
            )
        ).scalars()
    )
    counts = {
        source_id: {
            "total": total,
            "pending": pending,
            "failed": failed,
            "synced": synced,
        }
        for source_id, total, pending, failed, synced in (
            await db.execute(
                select(
                    WebDAVEntry.source_id,
                    func.count(WebDAVEntry.id),
                    func.count(WebDAVEntry.id).filter(
                        WebDAVEntry.state.in_(("discovered", "changed"))
                    ),
                    func.count(WebDAVEntry.id).filter(WebDAVEntry.state == "failed"),
                    func.count(WebDAVEntry.id).filter(WebDAVEntry.state == "synced"),
                ).group_by(WebDAVEntry.source_id)
            )
        ).all()
    }
    result = []
    for row in rows:
        item = row.to_public_dict()
        item["entry_counts"] = counts.get(
            row.id, {"total": 0, "pending": 0, "failed": 0, "synced": 0}
        )
        result.append(item)
    return result


@router.get("/{source_id}/entries", response_model=list[dict[str, Any]])
async def list_entries(source_id: int, db: AsyncSession = Depends(get_db)):
    await _source_or_404(db, source_id)
    entries = (
        (
            await db.execute(
                select(WebDAVEntry)
                .where(WebDAVEntry.source_id == source_id)
                .order_by(WebDAVEntry.remote_path)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": entry.id,
            "remote_path": entry.remote_path,
            "file_size": entry.file_size,
            "state": entry.state,
            "document_id": entry.document_id,
            "last_error": entry.last_error,
            "synced_at": entry.synced_at.isoformat() if entry.synced_at else None,
        }
        for entry in entries
    ]


@router.post("", response_model=dict[str, Any], status_code=201)
async def create_source(payload: SourceCreate, db: AsyncSession = Depends(get_db)):
    try:
        base_url = validate_webdav_url(
            payload.base_url,
            trusted_private_network=payload.trusted_private_network,
        )
    except URLSecurityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    source = WebDAVSource(
        name=payload.name.strip(),
        base_url=base_url,
        username=payload.username,
        password_cipher=encrypt_secret(payload.password),
        has_password=bool(payload.password),
        root_path="/" + payload.root_path.strip("/"),
        recursive=payload.recursive,
        trusted_private_network=payload.trusted_private_network,
        include_extensions=payload.include_extensions,
        ignore_patterns=payload.ignore_patterns,
        sync_status="idle",
    )
    db.add(source)
    await db.commit()
    await db.refresh(source)
    return source.to_public_dict()


@router.patch("/{source_id}", response_model=dict[str, Any])
async def update_source(
    source_id: int,
    payload: SourceUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update a connector while preserving its imported documents and password."""

    source = await _source_or_404(db, source_id)
    if payload.clear_password and payload.password:
        raise HTTPException(
            status_code=400,
            detail="不能同时填写新密码和选择清除密码",
        )
    try:
        base_url = validate_webdav_url(
            payload.base_url,
            trusted_private_network=payload.trusted_private_network,
        )
    except URLSecurityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    previous_scan_config = (
        source.base_url,
        source.root_path,
        bool(source.recursive),
        tuple(source.include_extensions or []),
        tuple(source.ignore_patterns or []),
    )
    source.name = payload.name.strip()
    source.base_url = base_url
    source.username = payload.username
    source.root_path = "/" + payload.root_path.strip("/")
    source.recursive = payload.recursive
    source.trusted_private_network = payload.trusted_private_network
    source.include_extensions = payload.include_extensions
    source.ignore_patterns = payload.ignore_patterns
    if payload.clear_password:
        source.password_cipher = encrypt_secret("")
        source.has_password = False
    elif payload.password:
        source.password_cipher = encrypt_secret(payload.password)
        source.has_password = True
    source.sync_status = "idle"
    source.last_error = None

    current_scan_config = (
        source.base_url,
        source.root_path,
        bool(source.recursive),
        tuple(source.include_extensions or []),
        tuple(source.ignore_patterns or []),
    )
    await db.commit()
    await db.refresh(source)
    result = source.to_public_dict()
    result["requires_rescan"] = previous_scan_config != current_scan_config
    return result


@router.delete("/{source_id}", response_model=dict[str, Any])
async def delete_source(source_id: int, db: AsyncSession = Depends(get_db)):
    """Remove a connector without deleting documents already indexed from it."""

    source = await _source_or_404(db, source_id)
    await db.delete(source)
    await db.commit()
    return {
        "ok": True,
        "message": "WebDAV 连接器已删除，已入库资料予以保留",
    }


@router.post("/{source_id}/test", response_model=dict[str, Any])
async def test_source(source_id: int, db: AsyncSession = Depends(get_db)):
    source = await _source_or_404(db, source_id)
    try:
        entries = await propfind(
            base_url=source.base_url,
            root_path=source.root_path,
            username=source.username,
            password=decrypt_secret(source.password_cipher),
            trusted_private_network=source.trusted_private_network,
        )
    except (WebDAVError, URLSecurityError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return {
        "ok": True,
        "message": "WebDAV 连接正常",
        "entry_count": len(entries),
    }


@router.post("/{source_id}/scan", response_model=dict[str, Any])
async def scan_source(source_id: int, db: AsyncSession = Depends(get_db)):
    source = await _source_or_404(db, source_id)
    source.sync_status = "scanning"
    source.last_error = None
    await db.commit()
    try:
        remote = await propfind(
            base_url=source.base_url,
            root_path=source.root_path,
            username=source.username,
            password=decrypt_secret(source.password_cipher),
            trusted_private_network=source.trusted_private_network,
        )
    except (WebDAVError, URLSecurityError) as exc:
        source.sync_status = "failed"
        source.last_error = str(exc)
        await db.commit()
        raise HTTPException(status_code=502, detail=str(exc)) from None

    now = datetime.now(timezone.utc)
    extensions = set(source.include_extensions or DEFAULT_EXTENSIONS)
    files = [
        entry
        for entry in remote
        if not entry.is_collection
        and PurePosixPath(entry.path).suffix.lower() in extensions
        and not _matches_ignore_pattern(entry.path, source.ignore_patterns or [])
    ]
    existing = {
        entry.remote_path: entry
        for entry in (
            await db.execute(
                select(WebDAVEntry).where(WebDAVEntry.source_id == source.id)
            )
        ).scalars()
    }
    discovered = changed = unchanged = ignored = 0
    seen: set[str] = set()
    for remote_entry in files:
        seen.add(remote_entry.path)
        entry = existing.get(remote_entry.path)
        if entry is None:
            entry = WebDAVEntry(
                source_id=source.id,
                remote_path=remote_entry.path,
                state="discovered",
            )
            db.add(entry)
            discovered += 1
        elif entry.state == "ignored":
            ignored += 1
        elif (
            entry.etag != remote_entry.etag
            or entry.last_modified != remote_entry.last_modified
            or entry.file_size != remote_entry.size
        ):
            entry.state = "changed"
            changed += 1
        else:
            unchanged += 1
        entry.etag = remote_entry.etag
        entry.last_modified = remote_entry.last_modified
        entry.file_size = remote_entry.size
        entry.content_type = remote_entry.content_type
        entry.last_seen_at = now
    missing = 0
    for path, entry in existing.items():
        if path not in seen and entry.state != "ignored":
            entry.state = "missing"
            missing += 1
    source.sync_status = "idle"
    source.last_scan_at = now
    await db.commit()
    return {
        "ok": True,
        "discovered": discovered,
        "changed": changed,
        "unchanged": unchanged,
        "ignored": ignored,
        "missing": missing,
        "eligible_files": len(files),
    }


def _matches_ignore_pattern(path: str, patterns: list[str]) -> bool:
    """Match WebDAV ignore globs against the full path and each path segment."""

    parts = PurePosixPath(path).parts
    return any(
        fnmatchcase(path, pattern)
        or any(fnmatchcase(part, pattern) for part in parts)
        for pattern in patterns
    )


@router.post("/{source_id}/sync", response_model=dict[str, Any])
async def sync_source(
    source_id: int,
    db: AsyncSession = Depends(get_db),
    storage: BlobStorage = Depends(get_storage),
):
    """Download new/changed files and enqueue the normal ingestion pipeline."""

    source = await _source_or_404(db, source_id)
    entries = (
        (
            await db.execute(
                select(WebDAVEntry)
                .where(
                    WebDAVEntry.source_id == source.id,
                    WebDAVEntry.state.in_(("discovered", "changed", "failed")),
                )
                .order_by(WebDAVEntry.id)
            )
        )
        .scalars()
        .all()
    )
    source.sync_status = "syncing"
    source.last_error = None
    await db.commit()
    imported = updated = unchanged = failed = 0
    password = decrypt_secret(source.password_cipher)
    max_bytes = settings.max_upload_size_mb * 1024 * 1024

    for entry in entries:
        try:
            payload, upstream_type = await download_file(
                base_url=source.base_url,
                remote_path=entry.remote_path,
                username=source.username,
                password=password,
                trusted_private_network=source.trusted_private_network,
                max_bytes=max_bytes,
            )
            filename = normalized_filename(PurePosixPath(entry.remote_path).name)
            content_type = validate_file_type(
                filename, upstream_type.split(";", 1)[0].strip()
            )
            stored = storage.save(BytesIO(payload), max_bytes=max_bytes)
            entry.content_hash = stored.sha256
            existing_blob = await db.scalar(
                select(Blob).where(Blob.sha256 == stored.sha256)
            )
            if existing_blob is not None:
                storage.delete(stored.storage_key)
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

            document = (
                await db.get(Document, entry.document_id)
                if entry.document_id is not None
                else None
            )
            if document is None:
                document = Document(
                    title=(PurePosixPath(filename).stem or filename)[:1024],
                    source_type=DocumentSourceType.file,
                    meta={
                        "external_source": "webdav",
                        "webdav_source_id": source.id,
                        "webdav_path": entry.remote_path,
                    },
                )
                db.add(document)
                await db.flush()
                version_number = 1
                imported += 1
            else:
                current = (
                    await db.get(DocumentVersion, document.current_version_id)
                    if document.current_version_id
                    else None
                )
                if current is not None and current.content_hash == stored.sha256:
                    entry.state = "synced"
                    entry.synced_at = datetime.now(timezone.utc)
                    unchanged += 1
                    await db.commit()
                    continue
                version_number = (
                    int(
                        (
                            await db.execute(
                                select(func.max(DocumentVersion.version_number)).where(
                                    DocumentVersion.document_id == document.id
                                )
                            )
                        ).scalar_one()
                        or 0
                    )
                    + 1
                )
                updated += 1
            version = DocumentVersion(
                document_id=document.id,
                blob_id=blob.id,
                version_number=version_number,
                content_hash=stored.sha256,
                processing_status="created",
                meta={
                    "external_source": "webdav",
                    "webdav_source_id": source.id,
                    "webdav_path": entry.remote_path,
                },
            )
            db.add(version)
            await db.flush()
            document.current_version_id = version.id
            entry.document_id = document.id
            entry.state = "synced"
            entry.synced_at = datetime.now(timezone.utc)
            entry.last_error = None
            db.add(
                ProcessingJob(
                    document_id=document.id,
                    document_version_id=version.id,
                    stage="stored",
                    status="created",
                    idempotency_key=f"{version.id}:stored:webdav-v1",
                    config_version="webdav-v1",
                )
            )
            await db.commit()
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            entry = await db.get(WebDAVEntry, entry.id)
            if entry is not None:
                entry.state = "failed"
                entry.last_error = str(exc)[:1000]
                await db.commit()
            failed += 1

    source = await db.get(WebDAVSource, source_id)
    assert source is not None
    source.sync_status = "failed" if failed else "idle"
    source.last_sync_at = datetime.now(timezone.utc)
    source.last_error = f"{failed} 个文件同步失败" if failed else None
    await db.commit()
    return {
        "ok": failed == 0,
        "imported": imported,
        "updated": updated,
        "unchanged": unchanged,
        "failed": failed,
    }
