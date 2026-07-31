from __future__ import annotations

from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.db import get_db
from ..models.blobs import Blob
from ..models.documents import Document, DocumentSourceType, DocumentVersion
from ..models.processing import ProcessingJob
from ..models.webdav import ExternalItemExclusion, WebDAVEntry, WebDAVSource
from ..security.secrets import decrypt_secret, encrypt_secret
from ..security.url_safety import URLSecurityError
from ..services.webdav import (
    WebDAVError,
    download_file,
    propfind,
    validate_webdav_url,
    webdav_external_identity,
)
from ..services.document_lifecycle import (
    restore_documents as restore_document_records,
    trash_documents as trash_document_records,
)
from ..storage import get_storage
from ..storage.base import BlobStorage
from .files import normalized_filename, validate_file_type

router = APIRouter(prefix="/webdav", tags=["webdav"])
DEFAULT_EXTENSIONS = [".pdf", ".doc", ".docx", ".md", ".markdown", ".txt"]
REMOTE_MISSING_CONFIRMATIONS = 2
REMOTE_MISSING_GRACE = timedelta(hours=24)


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
    remote_delete_policy: Literal["trash", "keep"] = "trash"

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


class SourceEnabledUpdate(BaseModel):
    is_enabled: bool


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
            "suspected_missing": suspected_missing,
            "missing": missing,
        }
        for source_id, total, pending, failed, synced, suspected_missing, missing in (
            await db.execute(
                select(
                    WebDAVEntry.source_id,
                    func.count(WebDAVEntry.id),
                    func.count(WebDAVEntry.id).filter(
                        WebDAVEntry.state.in_(("discovered", "changed"))
                    ),
                    func.count(WebDAVEntry.id).filter(WebDAVEntry.state == "failed"),
                    func.count(WebDAVEntry.id).filter(WebDAVEntry.state == "synced"),
                    func.count(WebDAVEntry.id).filter(
                        WebDAVEntry.state == "suspected_missing"
                    ),
                    func.count(WebDAVEntry.id).filter(
                        or_(
                            WebDAVEntry.state == "missing",
                            (
                                (WebDAVEntry.state == "ignored")
                                & (WebDAVEntry.resume_state == "missing")
                                & (
                                    WebDAVEntry.ignore_reason
                                    == "document_trashed"
                                )
                            ),
                        )
                    ),
                ).group_by(WebDAVEntry.source_id)
            )
        ).all()
    }
    result = []
    for row in rows:
        item = row.to_public_dict()
        item["entry_counts"] = counts.get(
            row.id,
            {
                "total": 0,
                "pending": 0,
                "failed": 0,
                "synced": 0,
                "suspected_missing": 0,
                "missing": 0,
            },
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
            "ignore_reason": entry.ignore_reason,
            "resume_state": entry.resume_state,
            "last_seen_at": (
                entry.last_seen_at.isoformat() if entry.last_seen_at else None
            ),
            "missing_since": (
                entry.missing_since.isoformat() if entry.missing_since else None
            ),
            "missing_count": entry.missing_count or 0,
            "keep_snapshot": bool(entry.keep_snapshot),
        }
        for entry in entries
    ]


@router.post(
    "/{source_id}/entries/{entry_id}/allow-reimport",
    response_model=dict[str, Any],
)
async def allow_entry_reimport(
    source_id: int,
    entry_id: int,
    db: AsyncSession = Depends(get_db),
):
    await _source_or_404(db, source_id)
    entry = await db.get(WebDAVEntry, entry_id)
    if entry is None or entry.source_id != source_id:
        raise HTTPException(status_code=404, detail="远端文件记录不存在")
    document = (
        await db.get(Document, entry.document_id)
        if entry.document_id is not None
        else None
    )
    if document is not None and document.is_deleted:
        raise HTTPException(
            status_code=409,
            detail="该资料仍在回收站，请先恢复或永久删除",
        )
    if entry.external_identity:
        await db.execute(
            delete(ExternalItemExclusion).where(
                ExternalItemExclusion.external_identity
                == entry.external_identity
            )
        )
    entry.state = "discovered"
    entry.ignore_reason = None
    entry.ignored_at = None
    entry.resume_state = None
    entry.missing_since = None
    entry.missing_count = 0
    entry.keep_snapshot = False
    await db.commit()
    return {"ok": True, "message": "已允许该文件重新入库"}


@router.post(
    "/{source_id}/entries/{entry_id}/keep-snapshot",
    response_model=dict[str, Any],
)
async def keep_entry_snapshot(
    source_id: int,
    entry_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Keep a local knowledge snapshot even when its remote file is absent."""

    await _source_or_404(db, source_id)
    entry = await db.get(WebDAVEntry, entry_id)
    if entry is None or entry.source_id != source_id:
        raise HTTPException(status_code=404, detail="远端文件记录不存在")
    document = (
        await db.get(Document, entry.document_id)
        if entry.document_id is not None
        else None
    )
    if document is None:
        raise HTTPException(status_code=409, detail="该远端文件还没有可保留的知识快照")
    entry.keep_snapshot = True
    if document.is_deleted:
        if document.delete_reason != "remote_missing":
            raise HTTPException(
                status_code=409,
                detail="该资料不是因远端删除进入回收站，请在回收站中恢复",
            )
        await restore_document_records(db, [document], commit=False)
    entry.state = "missing"
    entry.resume_state = None
    entry.ignore_reason = None
    entry.ignored_at = None
    meta = dict(document.meta or {})
    meta["webdav_keep_snapshot"] = True
    document.meta = meta
    await db.commit()
    return {"ok": True, "message": "已保留为本地知识快照，不再随远端删除回收"}


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
        remote_delete_policy=payload.remote_delete_policy,
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
    previous_base_url = source.base_url
    source.name = payload.name.strip()
    source.base_url = base_url
    source.username = payload.username
    source.root_path = "/" + payload.root_path.strip("/")
    source.recursive = payload.recursive
    source.trusted_private_network = payload.trusted_private_network
    source.include_extensions = payload.include_extensions
    source.ignore_patterns = payload.ignore_patterns
    source.remote_delete_policy = payload.remote_delete_policy
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
    if previous_base_url != source.base_url:
        await db.execute(
            delete(WebDAVEntry).where(WebDAVEntry.source_id == source.id)
        )
    await db.commit()
    await db.refresh(source)
    result = source.to_public_dict()
    result["requires_rescan"] = previous_scan_config != current_scan_config
    return result


@router.get("/{source_id}/delete-impact", response_model=dict[str, Any])
async def source_delete_impact(
    source_id: int,
    db: AsyncSession = Depends(get_db),
):
    source = await _source_or_404(db, source_id)
    linked_document_ids = list(
        (
            await db.execute(
                select(WebDAVEntry.document_id)
                .where(
                    WebDAVEntry.source_id == source.id,
                    WebDAVEntry.document_id.is_not(None),
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    active = trash = 0
    if linked_document_ids:
        active = int(
            (
                await db.execute(
                    select(func.count(Document.id)).where(
                        Document.id.in_(linked_document_ids),
                        Document.is_deleted.is_(False),
                    )
                )
            ).scalar_one()
            or 0
        )
        trash = int(
            (
                await db.execute(
                    select(func.count(Document.id)).where(
                        Document.id.in_(linked_document_ids),
                        Document.is_deleted.is_(True),
                    )
                )
            ).scalar_one()
            or 0
        )
    return {
        "source_id": source.id,
        "source_name": source.name,
        "entry_count": int(
            (
                await db.execute(
                    select(func.count(WebDAVEntry.id)).where(
                        WebDAVEntry.source_id == source.id
                    )
                )
            ).scalar_one()
            or 0
        ),
        "active_document_count": active,
        "trashed_document_count": trash,
        "remote_files_affected": 0,
    }


@router.patch("/{source_id}/enabled", response_model=dict[str, Any])
async def set_source_enabled(
    source_id: int,
    payload: SourceEnabledUpdate,
    db: AsyncSession = Depends(get_db),
):
    source = await _source_or_404(db, source_id)
    source.is_enabled = payload.is_enabled
    source.sync_status = "idle"
    source.last_error = None
    await db.commit()
    await db.refresh(source)
    return source.to_public_dict()


@router.delete("/{source_id}", response_model=dict[str, Any])
async def delete_source(
    source_id: int,
    document_action: Literal["keep", "trash"] = Query("keep"),
    db: AsyncSession = Depends(get_db),
):
    """Remove a connector, optionally moving its active knowledge to trash."""

    source = await _source_or_404(db, source_id)
    linked_ids = list(
        (
            await db.execute(
                select(WebDAVEntry.document_id)
                .where(
                    WebDAVEntry.source_id == source.id,
                    WebDAVEntry.document_id.is_not(None),
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    documents = (
        list(
            (
                await db.execute(
                    select(Document).where(Document.id.in_(linked_ids))
                )
            )
            .scalars()
            .all()
        )
        if linked_ids
        else []
    )
    if document_action == "trash":
        await trash_document_records(
            db,
            documents,
            reason="connector_removed",
            commit=False,
        )
    for document in documents:
        meta = dict(document.meta or {})
        meta["webdav_source_name"] = source.name
        meta["webdav_source_id"] = None
        meta["connector_removed"] = True
        document.meta = meta
    await db.delete(source)
    await db.commit()
    return {
        "ok": True,
        "affected_documents": len(documents),
        "document_action": document_action,
        "message": (
            "WebDAV 连接器已删除，关联资料已移入回收站"
            if document_action == "trash"
            else "WebDAV 连接器已删除，已入库资料予以保留"
        ),
        "remote_files_affected": 0,
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
    if not source.is_enabled:
        raise HTTPException(status_code=409, detail="知识源已停用，请先启用")
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
    remote_file_paths = {
        entry.path for entry in remote if not entry.is_collection
    }
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
    linked_document_ids = {
        entry.document_id
        for entry in existing.values()
        if entry.document_id is not None
    }
    linked_documents = (
        {
            document.id: document
            for document in (
                await db.execute(
                    select(Document).where(Document.id.in_(linked_document_ids))
                )
            )
            .scalars()
            .all()
        }
        if linked_document_ids
        else {}
    )
    discovered = changed = unchanged = ignored = restored = 0
    seen: set[str] = set()
    for remote_entry in files:
        seen.add(remote_entry.path)
        identity = webdav_external_identity(source.base_url, remote_entry.path)
        entry = existing.get(remote_entry.path)
        if entry is None:
            document = await db.scalar(
                select(Document).where(Document.external_identity == identity)
            )
            exclusion = await db.scalar(
                select(ExternalItemExclusion).where(
                    ExternalItemExclusion.external_identity == identity
                )
            )
            entry = WebDAVEntry(
                source_id=source.id,
                remote_path=remote_entry.path,
                external_identity=identity,
                document_id=document.id if document is not None else None,
                state=(
                    "ignored"
                    if exclusion is not None
                    or (document is not None and document.is_deleted)
                    else "discovered"
                ),
            )
            if exclusion is not None:
                entry.ignore_reason = exclusion.reason
                entry.ignored_at = now
            elif document is not None and document.is_deleted:
                entry.ignore_reason = "document_trashed"
                entry.resume_state = "discovered"
                entry.ignored_at = now
            db.add(entry)
            if entry.state == "ignored":
                ignored += 1
            else:
                discovered += 1
        elif entry.state == "ignored":
            document = linked_documents.get(entry.document_id)
            if (
                entry.ignore_reason == "document_trashed"
                and document is not None
                and document.is_deleted
                and document.delete_reason == "remote_missing"
            ):
                await restore_document_records(db, [document], commit=False)
                entry.state = "changed"
                entry.keep_snapshot = False
                restored += 1
                changed += 1
            else:
                ignored += 1
        elif (
            entry.state in ("missing", "suspected_missing")
            or
            entry.etag != remote_entry.etag
            or entry.last_modified != remote_entry.last_modified
            or entry.file_size != remote_entry.size
        ):
            entry.state = "changed"
            changed += 1
        else:
            unchanged += 1
        entry.etag = remote_entry.etag
        entry.external_identity = identity
        entry.last_modified = remote_entry.last_modified
        entry.file_size = remote_entry.size
        entry.content_type = remote_entry.content_type
        entry.last_seen_at = now
        entry.missing_since = None
        entry.missing_count = 0
        if entry.state != "ignored":
            entry.keep_snapshot = False
    suspected_missing = confirmed_missing = trashed = 0
    documents_to_trash: list[Document] = []
    for path, entry in existing.items():
        # A file excluded by a changed extension or ignore rule still exists
        # remotely and must never be interpreted as a remote deletion.
        if path in remote_file_paths or entry.state == "ignored":
            continue
        if entry.missing_since is None:
            entry.missing_since = now
            entry.missing_count = 1
            entry.state = "suspected_missing"
            suspected_missing += 1
            continue
        entry.missing_count = (entry.missing_count or 0) + 1
        missing_since = entry.missing_since
        if missing_since.tzinfo is None:
            missing_since = missing_since.replace(tzinfo=timezone.utc)
        confirmed = (
            entry.missing_count >= REMOTE_MISSING_CONFIRMATIONS
            and now - missing_since >= REMOTE_MISSING_GRACE
        )
        if not confirmed:
            entry.state = "suspected_missing"
            suspected_missing += 1
            continue
        entry.state = "missing"
        confirmed_missing += 1
        document = linked_documents.get(entry.document_id)
        if (
            source.remote_delete_policy == "trash"
            and not entry.keep_snapshot
            and document is not None
            and not document.is_deleted
        ):
            documents_to_trash.append(document)
    if documents_to_trash:
        trashed = await trash_document_records(
            db,
            documents_to_trash,
            reason="remote_missing",
            commit=False,
        )
    source.sync_status = "idle"
    source.last_scan_at = now
    await db.commit()
    return {
        "ok": True,
        "discovered": discovered,
        "changed": changed,
        "unchanged": unchanged,
        "ignored": ignored,
        "suspected_missing": suspected_missing,
        "missing": confirmed_missing,
        "trashed": trashed,
        "restored": restored,
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
    if not source.is_enabled:
        raise HTTPException(status_code=409, detail="知识源已停用，请先启用")
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
                        "webdav_source_name": source.name,
                        "webdav_path": entry.remote_path,
                    },
                    external_identity=entry.external_identity
                    or webdav_external_identity(
                        source.base_url, entry.remote_path
                    ),
                )
                db.add(document)
                await db.flush()
                version_number = 1
                imported += 1
            else:
                if document.is_deleted:
                    entry.state = "ignored"
                    entry.ignore_reason = "document_trashed"
                    entry.ignored_at = datetime.now(timezone.utc)
                    await db.commit()
                    continue
                document.external_identity = (
                    entry.external_identity
                    or document.external_identity
                    or webdav_external_identity(
                        source.base_url, entry.remote_path
                    )
                )
                document_meta = dict(document.meta or {})
                document_meta.update(
                    {
                        "external_source": "webdav",
                        "webdav_source_id": source.id,
                        "webdav_source_name": source.name,
                        "webdav_path": entry.remote_path,
                        "connector_removed": False,
                    }
                )
                document.meta = document_meta
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
            entry.ignore_reason = None
            entry.ignored_at = None
            entry.resume_state = None
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
