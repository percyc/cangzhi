"""Shared, per-file WebDAV import persistence plus a cursor-driven batch runner.

This is the single place that turns one remote WebDAV file into a durable
local document/version. Both the legacy ``POST /{source_id}/sync`` endpoint
and the new ``POST /{source_id}/sync-batch`` endpoint reuse the same logic so
they cannot drift. HTTP/worker/CLI never re-implement the file-persistence
rules here.

All functions operate on an immutable :class:`ImportSource` snapshot (never a
live ORM row) so an expired source after rollback/commit can never be reused
across a transaction boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import PurePosixPath

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.files import normalized_filename, validate_file_type
from ..models.blobs import Blob
from ..models.documents import Document, DocumentSourceType, DocumentVersion
from ..models.processing import ProcessingJob
from ..models.webdav import WebDAVEntry
from ..storage.base import BlobStorage
from .webdav import WebDAVError, in_scan_scope, webdav_external_identity
from .webdav_browser import resolve_directory

# States that are allowed to be pulled into the importer.
ELIGIBLE_STATES = ("discovered", "changed", "failed")
# Bounded how many candidate rows a single (short) batch request may evaluate,
# kept small so one request can never materialize a quadratic candidate list.
SCAN_CAP = 128


@dataclass(frozen=True)
class ImportSource:
    """Immutable snapshot of the WebDAV source fields a file import needs."""

    id: int
    name: str
    base_url: str
    root_path: str
    username: str
    trusted_private_network: bool
    recursive: bool
    ignore_patterns: tuple[str, ...]


def snapshot_source(source) -> ImportSource:
    return ImportSource(
        id=source.id,
        name=source.name,
        base_url=source.base_url,
        root_path=source.root_path,
        username=source.username,
        trusted_private_network=bool(source.trusted_private_network),
        recursive=bool(source.recursive),
        ignore_patterns=tuple(source.ignore_patterns or []),
    )


@dataclass(frozen=True)
class SingleImportOutcome:
    entry_id: int
    # one of: imported, updated, unchanged, ignored, failed
    outcome: str
    message: str | None = None


def sanitize_import_error(exc: BaseException) -> str:
    """Return a safe, per-file error string that never leaks secrets/content.

    Only details that are known safe (validated file-type strings from
    ``HTTPException`` and crafted ``WebDAVError`` messages) are surfaced for
    the specific file; any unknown class collapses to a generic message.
    ``ValueError`` is deliberately NOT whitelisted because ``str`` may include
    credentials or other sensitive upstream text.
    """
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, str) and detail:
            return detail
    if isinstance(exc, WebDAVError):
        return str(exc)
    return "该文件导入失败，请重试或检查来源"


def _escape_like(value: str) -> str:
    """Escape LIKE metacharacters so a decoded path is matched literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def import_single_entry(
    db: AsyncSession,
    *,
    source: ImportSource,
    entry: WebDAVEntry,
    storage: BlobStorage,
    download_file,
    spreadsheet_schema_is_stale,
    password: str,
    max_bytes: int,
) -> SingleImportOutcome:
    """Persist one remote file, committing exactly once on success.

    ``entry_id`` is captured up front and, on failure, the entry is re-loaded
    fresh *after* rollback so expired ORM rows are never carried across a
    transaction boundary (MissingGreenlet hazard). Physical blob storage is
    cleaned up when the transaction rolls back. Excluded / deleted /
    keep-snapshot files are never downloaded.
    """
    entry_id = entry.id
    remote_path = entry.remote_path
    external_identity = entry.external_identity or webdav_external_identity(
        source.base_url, remote_path
    )

    document = (
        (await db.get(Document, entry.document_id))
        if entry.document_id is not None
        else None
    )
    # Respect deleted / excluded / keep-snapshot: never download or delete
    # for docs that should no longer be tracked as importable.
    if (document is not None and document.is_deleted) or entry.keep_snapshot:
        entry.state = "ignored"
        entry.ignore_reason = (
            "document_trashed"
            if document is not None and document.is_deleted
            else "keep_snapshot"
        )
        entry.ignored_at = _now()
        await db.commit()
        return SingleImportOutcome(entry_id=entry_id, outcome="ignored")

    saved_key: str | None = None
    try:
        payload, upstream_type = await download_file(
            base_url=source.base_url,
            remote_path=remote_path,
            username=source.username,
            password=password,
            trusted_private_network=source.trusted_private_network,
            max_bytes=max_bytes,
        )
        filename = normalized_filename(PurePosixPath(remote_path).name)
        content_type = validate_file_type(filename, upstream_type)
        stored = storage.save(BytesIO(payload), max_bytes=max_bytes)
        saved_key = stored.storage_key
        existing_blob = await db.scalar(
            select(Blob).where(Blob.sha256 == stored.sha256)
        )
        if existing_blob is not None:
            storage.delete(stored.storage_key)
            saved_key = None
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

        outcome = await _apply_import(
            db,
            source=source,
            entry=entry,
            document=document,
            blob=blob,
            stored=stored,
            filename=filename,
            remote_path=remote_path,
            external_identity=external_identity,
            spreadsheet_schema_is_stale=spreadsheet_schema_is_stale,
        )
        await db.commit()
        saved_key = None
        return SingleImportOutcome(entry_id=entry_id, outcome=outcome)
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        if saved_key is not None:
            try:
                storage.delete(saved_key)
            except Exception:  # best-effort cleanup only
                pass
        fresh = await db.get(WebDAVEntry, entry_id)
        message = sanitize_import_error(exc)
        if fresh is not None:
            fresh.state = "failed"
            fresh.last_error = message[:1000]
            await db.commit()
        return SingleImportOutcome(entry_id=entry_id, outcome="failed", message=message)


async def _apply_import(
    db: AsyncSession,
    *,
    source: ImportSource,
    entry: WebDAVEntry,
    document: Document | None,
    blob: Blob,
    stored,
    filename: str,
    remote_path: str,
    external_identity: str,
    spreadsheet_schema_is_stale,
) -> str:
    now = _now()
    if document is None:
        document = Document(
            title=(PurePosixPath(filename).stem or filename)[:1024],
            source_type=DocumentSourceType.file,
            meta={
                "external_source": "webdav",
                "webdav_source_id": source.id,
                "webdav_source_name": source.name,
                "webdav_path": remote_path,
            },
            external_identity=external_identity,
        )
        db.add(document)
        await db.flush()
        await _commit_new_version(
            db,
            source=source,
            entry=entry,
            document=document,
            blob=blob,
            stored=stored,
            remote_path=remote_path,
            external_identity=external_identity,
            version_number=1,
        )
        return "imported"

    document.external_identity = document.external_identity or external_identity
    document_meta = dict(document.meta or {})
    document_meta.update(
        {
            "external_source": "webdav",
            "webdav_source_id": source.id,
            "webdav_source_name": source.name,
            "webdav_path": remote_path,
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
        if spreadsheet_schema_is_stale(current, filename):
            # Same bytes but an obsolete spreadsheet schema: re-parse in place.
            current.blob_id = blob.id
            current.processing_status = "created"
            parse_job = await db.scalar(
                select(ProcessingJob)
                .where(
                    ProcessingJob.document_version_id == current.id,
                    ProcessingJob.stage.in_(("stored", "parsing")),
                )
                .order_by(ProcessingJob.id)
            )
            if parse_job is None:
                parse_job = ProcessingJob(
                    document_id=document.id,
                    document_version_id=current.id,
                    stage="stored",
                    status="created",
                    idempotency_key=f"{current.id}:stored:webdav-spreadsheet-v2",
                    config_version="2",
                )
            else:
                parse_job.stage = "stored"
                parse_job.status = "created"
                parse_job.retry_count = 0
                parse_job.next_retry_at = None
                parse_job.last_error = None
                parse_job.error_details = None
                parse_job.config_version = "2"
                parse_job.started_at = None
                parse_job.finished_at = None
            db.add(parse_job)
            entry.state = "synced"
            entry.synced_at = now
            entry.last_error = None
            return "updated"
        entry.state = "synced"
        entry.synced_at = now
        entry.last_error = None
        return "unchanged"

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
    await _commit_new_version(
        db,
        source=source,
        entry=entry,
        document=document,
        blob=blob,
        stored=stored,
        remote_path=remote_path,
        external_identity=external_identity,
        version_number=version_number,
    )
    return "updated"


async def _commit_new_version(
    db: AsyncSession,
    *,
    source: ImportSource,
    entry: WebDAVEntry,
    document: Document,
    blob: Blob,
    stored,
    remote_path: str,
    external_identity: str,
    version_number: int,
) -> None:
    now = _now()
    version = DocumentVersion(
        document_id=document.id,
        blob_id=blob.id,
        version_number=version_number,
        content_hash=stored.sha256,
        processing_status="created",
        meta={
            "external_source": "webdav",
            "webdav_source_id": source.id,
            "webdav_path": remote_path,
        },
    )
    db.add(version)
    await db.flush()
    document.current_version_id = version.id
    entry.document_id = document.id
    entry.external_identity = external_identity
    entry.state = "synced"
    entry.ignore_reason = None
    entry.ignored_at = None
    entry.resume_state = None
    entry.synced_at = now
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


# ---------------------------------------------------------------------------
# Cursor-driven batch
# ---------------------------------------------------------------------------


async def _freeze_through(db: AsyncSession, source_id: int, through_id: int | None) -> int:
    """Freeze ``through_id`` to the maximum source entry id on the first batch."""
    if through_id is not None:
        return through_id
    max_id = (
        await db.execute(
            select(func.max(WebDAVEntry.id)).where(WebDAVEntry.source_id == source_id)
        )
    ).scalar_one()
    return int(max_id) if max_id else 0


def _directory_clause(directory: str | None):
    """Prefix match on a segment boundary; the root path ``/`` stays ``/``."""
    if directory is None:
        return None
    prefix = directory if directory.endswith("/") else directory + "/"
    return or_(
        WebDAVEntry.remote_path == directory,
        WebDAVEntry.remote_path.like(_escape_like(prefix) + "%", escape="\\"),
    )


def _candidate_stmt(
    *,
    source_id: int,
    after_id: int,
    through_id: int,
    entry_ids: list[int] | None,
    directory: str | None,
    failed_only: bool,
):
    states = ("failed",) if failed_only else ELIGIBLE_STATES
    stmt = select(WebDAVEntry).where(
        WebDAVEntry.source_id == source_id,
        WebDAVEntry.state.in_(states),
        WebDAVEntry.id > after_id,
    )
    if through_id is not None:
        stmt = stmt.where(WebDAVEntry.id <= through_id)
    if entry_ids is not None:
        stmt = stmt.where(WebDAVEntry.id.in_(entry_ids))
    clause = _directory_clause(directory)
    if clause is not None:
        stmt = stmt.where(clause)
    return stmt.order_by(WebDAVEntry.id)


def _is_scope_file(
    entry: WebDAVEntry, source: ImportSource, root: str, extensions: set[str]
) -> bool:
    path = entry.remote_path
    if PurePosixPath(path).suffix.lower() not in extensions:
        return False
    return in_scan_scope(path, root, source.recursive, list(source.ignore_patterns))


async def _evaluate_candidates(
    db: AsyncSession,
    rows,
    *,
    source: ImportSource,
    root: str,
    extensions: set[str],
) -> tuple[WebDAVEntry | None, int]:
    """Pick the first importable row, skipping ineligible ones (no N+1 probes).

    Linked documents are loaded once for the whole candidate batch rather than
    one query per candidate row.
    """
    document_ids = {entry.document_id for entry in rows if entry.document_id is not None}
    documents = {}
    if document_ids:
        for document in (
            await db.scalars(select(Document).where(Document.id.in_(document_ids)))
        ).all():
            documents[document.id] = document
    skipped = 0
    for entry in rows:
        if not _is_scope_file(entry, source, root, extensions):
            skipped += 1
            continue
        if entry.keep_snapshot:
            skipped += 1
            continue
        if entry.document_id is not None:
            document = documents.get(entry.document_id)
            if document is None or document.is_deleted:
                skipped += 1
                continue
        return entry, skipped
    return None, skipped


async def _has_remaining(
    db: AsyncSession,
    *,
    source_id: int,
    after_id: int,
    through_id: int,
    entry_ids: list[int] | None,
    directory: str | None,
    failed_only: bool,
) -> bool:
    """Single bounded existence check for any further SQL-eligible candidate."""
    stmt = select(WebDAVEntry.id).where(
        WebDAVEntry.source_id == source_id,
        WebDAVEntry.id > after_id,
    )
    states = ("failed",) if failed_only else ELIGIBLE_STATES
    stmt = stmt.where(WebDAVEntry.state.in_(states))
    if through_id is not None:
        stmt = stmt.where(WebDAVEntry.id <= through_id)
    if entry_ids is not None:
        stmt = stmt.where(WebDAVEntry.id.in_(entry_ids))
    clause = _directory_clause(directory)
    if clause is not None:
        stmt = stmt.where(clause)
    return (await db.scalar(stmt.limit(1))) is not None


def _build_result(
    *,
    ok: bool,
    imported: int,
    updated: int,
    unchanged: int,
    failed: int,
    skipped: int,
    processed: int,
    after_id: int,
    through_id: int,
    has_more: bool,
    errors: list[dict],
) -> dict:
    return {
        "ok": ok,
        "imported": imported,
        "updated": updated,
        "unchanged": unchanged,
        "failed": failed,
        "skipped": skipped,
        "processed": processed,
        "after_id": after_id,
        "through_id": through_id,
        "has_more": has_more,
        "errors": errors,
    }


async def run_sync_batch(
    db: AsyncSession,
    *,
    source: ImportSource,
    storage: BlobStorage,
    extensions: set[str],
    download_file,
    spreadsheet_schema_is_stale,
    password: str,
    max_bytes: int,
    after_id: int = 0,
    through_id: int | None = None,
    entry_ids: list[int] | None = None,
    directory: str | None = None,
    failed_only: bool = False,
    limit: int = 1,
) -> dict:
    """Process at most one eligible file in a short, cursor-driven request."""
    # Union is not required: an empty explicit id list selects nothing, never all.
    if entry_ids is not None and not entry_ids:
        frozen = await _freeze_through(db, source.id, through_id)
        return _build_result(
            ok=True, imported=0, updated=0, unchanged=0, failed=0,
            skipped=0, processed=0, after_id=after_id, through_id=frozen,
            has_more=False, errors=[],
        )

    # Reuses the same canonical validation as the browser: absolute path, no
    # traversal, must be the scan root or a descendant on a segment boundary.
    root, directory = resolve_directory(source, directory)
    frozen_through = await _freeze_through(db, source.id, through_id)

    rows = list(
        (
            await db.scalars(
                _candidate_stmt(
                    source_id=source.id,
                    after_id=after_id,
                    through_id=frozen_through,
                    entry_ids=entry_ids,
                    directory=directory,
                    failed_only=failed_only,
                ).limit(SCAN_CAP)
            )
        ).all()
    )
    chosen, skipped = await _evaluate_candidates(
        db, rows, source=source, root=root, extensions=extensions
    )

    imported = updated = unchanged = failed = 0
    processed = 0
    errors: list[dict] = []
    if chosen is not None:
        processed = 1
        outcome = await import_single_entry(
            db,
            source=source,
            entry=chosen,
            storage=storage,
            download_file=download_file,
            spreadsheet_schema_is_stale=spreadsheet_schema_is_stale,
            password=password,
            max_bytes=max_bytes,
        )
        # Cursor advances past the processed entry even on failure so a bad
        # file is never retried in the default mode (explicit retry is required).
        after_id = outcome.entry_id
        if outcome.outcome == "imported":
            imported += 1
        elif outcome.outcome == "updated":
            updated += 1
        elif outcome.outcome == "unchanged":
            unchanged += 1
        elif outcome.outcome == "failed":
            failed += 1
            if outcome.message:
                errors.append({"entry_id": outcome.entry_id, "message": outcome.message})
    elif rows:
        # Nothing eligible in the evaluated window: advance the cursor past the
        # skipped rows so a large ineligible block is never re-scanned. has_more
        # then reflects a fresh bounded lookahead instead of a silent truncation.
        after_id = rows[-1].id

    has_more = await _has_remaining(
        db,
        source_id=source.id,
        after_id=after_id,
        through_id=frozen_through,
        entry_ids=entry_ids,
        directory=directory,
        failed_only=failed_only,
    )
    return _build_result(
        ok=failed == 0,
        imported=imported,
        updated=updated,
        unchanged=unchanged,
        failed=failed,
        skipped=skipped,
        processed=processed,
        after_id=after_id,
        through_id=frozen_through,
        has_more=has_more,
        errors=errors,
    )
