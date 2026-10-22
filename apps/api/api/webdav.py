from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.db import get_db
from ..models.documents import Document, DocumentVersion
from ..models.webdav import ExternalItemExclusion, WebDAVEntry, WebDAVSource, WebDAVScan
from ..security.secrets import decrypt_secret, encrypt_secret
from ..security.url_safety import URLSecurityError
from ..services.document_lifecycle import (
    restore_documents as restore_document_records,
)
from ..services.document_lifecycle import (
    trash_documents as trash_document_records,
)
from ..services.webdav import (
    WebDAVError,
    download_file,
    propfind,
    scan_tree,
    matches_ignore_pattern as _matches_ignore_pattern,
    validate_webdav_url,
    webdav_external_identity,
)
from ..storage import get_storage
from ..services.webdav_import import (
    import_single_entry,
    run_sync_batch,
    snapshot_source,
)
from ..services.webdav_reconciliation import reconcile_scan
from ..services.webdav_scan_jobs import (
    ScanConflict, cancel_scan, current_scan, ensure_not_scanning, public_scan, start_scan,
)
from ..storage.base import BlobStorage

router = APIRouter(prefix="/webdav", tags=["webdav"])


async def _scan_guard(db, source_id):
    try:
        await ensure_not_scanning(db, source_id)
    except ScanConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/{source_id}/scan-jobs", status_code=202)
async def enqueue_scan(source_id: int, resume: bool = False, db: AsyncSession = Depends(get_db)):
    await _source_or_404(db, source_id)
    try:
        return await start_scan(db, source_id, resume=resume)
    except ScanConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.get("/{source_id}/scan-jobs")
async def scan_progress(source_id: int, db: AsyncSession = Depends(get_db)):
    await _source_or_404(db, source_id)
    return public_scan(await current_scan(db, source_id))


@router.post("/{source_id}/scan-jobs/cancel")
async def stop_scan(source_id: int, db: AsyncSession = Depends(get_db)):
    await _source_or_404(db, source_id)
    return await cancel_scan(db, source_id)


DEFAULT_EXTENSIONS = [
    ".pdf",
    ".doc",
    ".docx",
    ".wps",
    ".xlsx",
    ".xls",
    ".md",
    ".markdown",
    ".txt",
]
REMOTE_MISSING_CONFIRMATIONS = 2
REMOTE_MISSING_GRACE = timedelta(hours=24)


def _spreadsheet_schema_is_stale(
    version: DocumentVersion,
    filename: str,
) -> bool:
    if PurePosixPath(filename).suffix.lower() not in {".xlsx", ".xls"}:
        return False
    structured = version.structured_content or {}
    metadata = structured.get("metadata") if isinstance(structured, dict) else {}
    return not isinstance(metadata, dict) or metadata.get(
        "spreadsheet_schema_version"
    ) != 2


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
    scans = {run.source_id: public_scan(run) for run in (
        await db.scalars(select(WebDAVScan).where(WebDAVScan.source_id.in_([row.id for row in rows])))
    ).all()}
    for row in rows:
        item = row.to_public_dict()
        item["scan"] = scans.get(row.id)
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
    await _scan_guard(db, source_id)
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
    await _scan_guard(db, source_id)
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
    await _scan_guard(db, source_id)
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
    await _scan_guard(db, source_id)
    if not source.is_enabled:
        raise HTTPException(status_code=409, detail="知识源已停用，请先启用")
    source.sync_status = "scanning"
    source.last_error = None
    await db.commit()
    try:
        remote, scanned_directories, scan_root = await scan_tree(
            base_url=source.base_url,
            root_path=source.root_path,
            username=source.username,
            password=decrypt_secret(source.password_cipher),
            trusted_private_network=source.trusted_private_network,
            recursive=source.recursive,
            ignore_patterns=source.ignore_patterns or [],
            list_directory=propfind,
        )
    except (WebDAVError, URLSecurityError) as exc:
        source.sync_status = "failed"
        source.last_error = str(exc)
        await db.commit()
        raise HTTPException(status_code=502, detail=str(exc)) from None

    return await reconcile_scan(
        db, source, remote, scan_root, scanned_directories,
        extensions=set(source.include_extensions or DEFAULT_EXTENSIONS),
        confirmations=REMOTE_MISSING_CONFIRMATIONS, grace=REMOTE_MISSING_GRACE,
    )


class SyncBatchRequest(BaseModel):
    """Per-file, cursor-driven import batch. Union is not required."""
    entry_ids: list[int] | None = Field(
        default=None, max_length=1000, description="显式条目 ID（与 directory 互斥；空列表不选中任何条目）"
    )
    directory: str | None = Field(
        default=None, max_length=2048, description="绝对解码路径，须在扫描根或其子目录段边界上"
    )
    failed_only: bool = False
    after_id: int = Field(default=0, ge=0)
    through_id: int | None = Field(default=None, ge=0)
    limit: int = Field(default=1, ge=1, le=1)

    @field_validator("entry_ids")
    @classmethod
    def _coerce_entry_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        if any(int(item) <= 0 for item in value):
            raise ValueError("条目 ID 必须为正整数")
        return [int(item) for item in value]


async def _lock_source_nowait_or_409(
    db: AsyncSession, source_id: int
) -> WebDAVSource:
    """Lock the source row without waiting; a competing writer means 409."""
    try:
        source = await db.scalar(
            select(WebDAVSource)
            .where(WebDAVSource.id == source_id)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )
    except Exception:  # noqa: BLE001 - lock-wait timeout / aborted txn
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="来源正被其他操作占用，请稍后重试"
        ) from None
    if source is None:
        raise HTTPException(status_code=404, detail="WebDAV 知识源不存在")
    return source


@router.post("/{source_id}/sync", response_model=dict[str, Any])
async def sync_source(
    source_id: int,
    db: AsyncSession = Depends(get_db),
    storage: BlobStorage = Depends(get_storage),
):
    """Download new/changed files and enqueue the normal ingestion pipeline.

    Shared per-file persistence lives in ``webdav_import.import_single_entry``;
    each file commits independently so a mid-list failure never strand the
    source in ``syncing`` and expired ORM rows are never reused after rollback.
    """

    source = await _source_or_404(db, source_id)
    await _scan_guard(db, source_id)
    if not source.is_enabled:
        raise HTTPException(status_code=409, detail="知识源已停用，请先启用")
    entry_ids = list(
        (
            await db.execute(
                select(WebDAVEntry.id)
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
    source_snapshot = snapshot_source(await db.get(WebDAVSource, source_id))
    password = decrypt_secret(source.password_cipher)
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    imported = updated = unchanged = failed = 0
    try:
        for entry_id in entry_ids:
            entry = await db.get(WebDAVEntry, entry_id)
            if entry is None or entry.state not in (
                "discovered",
                "changed",
                "failed",
            ):
                continue
            outcome = await import_single_entry(
                db,
                source=source_snapshot,
                entry=entry,
                storage=storage,
                download_file=download_file,
                spreadsheet_schema_is_stale=_spreadsheet_schema_is_stale,
                password=password,
                max_bytes=max_bytes,
            )
            if outcome.outcome == "imported":
                imported += 1
            elif outcome.outcome == "updated":
                updated += 1
            elif outcome.outcome == "unchanged":
                unchanged += 1
            elif outcome.outcome == "failed":
                failed += 1
    finally:
        # Re-lock the source so no scan can change sync_status between our
        # reload and the commit; reload with populate_existing to drop any
        # row expired by the per-file commits.
        source = await db.scalar(
            select(WebDAVSource)
            .where(WebDAVSource.id == source_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if source is not None:
            now = datetime.now(timezone.utc)
            failed_count = int(
                (
                    await db.scalar(
                        select(func.count(WebDAVEntry.id)).where(
                            WebDAVEntry.source_id == source_id,
                            WebDAVEntry.state == "failed",
                        )
                    )
                    or 0
                )
            )
            if source.sync_status != "scanning":
                source.sync_status = "failed" if failed_count else "idle"
                source.last_error = (
                    f"{failed_count} 个文件同步失败" if failed_count else None
                )
            source.last_sync_at = now
            await db.commit()
    return {
        "ok": failed == 0,
        "imported": imported,
        "updated": updated,
        "unchanged": unchanged,
        "failed": failed,
    }


@router.post("/{source_id}/sync-batch", response_model=dict[str, Any])
async def sync_batch(
    source_id: int,
    payload: SyncBatchRequest,
    db: AsyncSession = Depends(get_db),
    storage: BlobStorage = Depends(get_storage),
):
    """Import at most one eligible file in a short, cursor-driven request.

    The source row lock is held ``nowait`` through the single-file network
    download and commit so partial import failures stay recoverable and the
    source is never left stuck in ``syncing``.
    """
    if payload.entry_ids is not None and payload.directory is not None:
        raise HTTPException(
            status_code=400, detail="entry_ids 与 directory 不能同时使用"
        )
    source = await _lock_source_nowait_or_409(db, source_id)
    await _scan_guard(db, source_id)
    if not source.is_enabled:
        raise HTTPException(status_code=409, detail="知识源已停用，请先启用")
    try:
        password = decrypt_secret(source.password_cipher)
    except Exception:  # noqa: BLE001
        raise HTTPException(
            status_code=400, detail="来源凭据无法解密，请重新保存连接配置"
        ) from None
    source_snapshot = snapshot_source(source)
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    try:
        result = await run_sync_batch(
            db,
            source=source_snapshot,
            storage=storage,
            extensions=set(source.include_extensions or DEFAULT_EXTENSIONS),
            download_file=download_file,
            spreadsheet_schema_is_stale=_spreadsheet_schema_is_stale,
            password=password,
            max_bytes=max_bytes,
            after_id=payload.after_id,
            through_id=payload.through_id,
            entry_ids=payload.entry_ids,
            directory=payload.directory,
            failed_only=payload.failed_only,
            limit=payload.limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    # Derive status from the actual failed-entry count on the source, not the
    # last single file, and never clobber a scan that started while the per-file
    # commit had released the source lock. Re-lock the source so the scan status
    # cannot change between our reload and the final commit; reload with
    # populate_existing to drop the row expired by the per-file commit.
    source = await db.scalar(
        select(WebDAVSource)
        .where(WebDAVSource.id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if source is not None:
        if result["processed"]:
            source.last_sync_at = datetime.now(timezone.utc)
        failed_count = int(
            (
                await db.scalar(
                    select(func.count(WebDAVEntry.id)).where(
                        WebDAVEntry.source_id == source_id,
                        WebDAVEntry.state == "failed",
                    )
                )
                or 0
            )
        )
        if source.sync_status != "scanning":
            source.sync_status = "failed" if failed_count else "idle"
            source.last_error = (
                f"{failed_count} 个文件导入失败" if failed_count else None
            )
        await db.commit()
    return result
