from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from sqlalchemy import select
from ..models.documents import Document
from ..models.webdav import ExternalItemExclusion, WebDAVEntry
from .document_lifecycle import restore_documents as restore_document_records, trash_documents as trash_document_records
from .webdav import in_scan_scope, matches_ignore_pattern as _matches_ignore_pattern, webdav_external_identity

async def reconcile_scan(db, source, remote, scan_root, scanned_directories, *,
                         extensions, confirm_missing=True,
                         confirmations=2, grace=timedelta(hours=24), commit=True):
    now = datetime.now(timezone.utc)
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
        if not confirm_missing:
            continue
        if not in_scan_scope(path, scan_root, source.recursive, source.ignore_patterns or []):
            continue
        if PurePosixPath(path).suffix.lower() not in extensions:
            continue
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
            entry.missing_count >= confirmations
            and now - missing_since >= grace
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
    if commit:
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
        "scanned_directories": scanned_directories,
    }

