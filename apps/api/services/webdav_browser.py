"""Bounded, database-paged view of the last completed WebDAV file inventory.

Folders are derived from known files, not a second live remote scan. No empty
directory or not-yet-scanned file is implied by this view.
"""
import posixpath

from sqlalchemy import case, func, literal, select, union_all

from .webdav import normalized_remote_path, scan_root_path
from ..models.webdav import WebDAVEntry


def resolve_directory(source, directory):
    root = scan_root_path(source.base_url, source.root_path)
    directory = root if directory is None else directory
    if not directory.startswith("/") or "\x00" in directory:
        raise ValueError("目录必须是来源范围内的绝对路径")
    normalized = normalized_remote_path(directory)
    if normalized != directory.rstrip("/") and not (normalized == directory == "/"):
        raise ValueError("目录路径无效")
    if normalized != root and not normalized.startswith(root.rstrip("/") + "/"):
        raise ValueError("目录不在当前来源范围内")
    return root, normalized


def entry_dict(entry):
    return {"kind": "file", "id": entry.id, "name": posixpath.basename(entry.remote_path),
            "remote_path": entry.remote_path, "file_size": entry.file_size,
            "state": entry.state, "document_id": entry.document_id,
            "last_error": entry.last_error, "ignore_reason": entry.ignore_reason,
            "resume_state": entry.resume_state, "missing_count": entry.missing_count,
            "keep_snapshot": bool(entry.keep_snapshot)}


async def browse_entries(db, source, *, directory=None, q="", state=None,
                         recursive=False, page=1, page_size=25):
    root, directory = resolve_directory(source, directory)
    prefix = directory.rstrip("/") + "/"
    # startswith escapes %, _ and the escape character; paths are decoded once
    # during scanning and must not be decoded a second time here.
    base = [WebDAVEntry.source_id == source.id,
            WebDAVEntry.remote_path.startswith(prefix, autoescape=True)]
    pending = WebDAVEntry.state.in_(("discovered", "changed"))
    failed = WebDAVEntry.state == "failed"
    counts = (await db.execute(select(
        func.count().filter(pending), func.count().filter(failed)
    ).select_from(WebDAVEntry).where(*base))).one()
    conditions = list(base)
    if q:
        conditions.append(WebDAVEntry.remote_path.contains(q, autoescape=True))
    if state:
        conditions.append(WebDAVEntry.state == state)
    relative = func.substr(WebDAVEntry.remote_path, len(prefix) + 1)
    is_nested = relative.contains("/")
    if db.bind.dialect.name == "postgresql":
        first_part = func.split_part(relative, "/", 1)
    else:
        first_part = func.substr(relative, 1, func.instr(relative, "/") - 1)
    files = select(literal("file").label("kind"),
                   WebDAVEntry.remote_path.label("remote_path"),
                   WebDAVEntry.id.label("entry_id"), literal(1).label("file_count"),
                   literal(0).label("pending_count"), literal(0).label("failed_count")
                   ).where(*conditions)
    if recursive or q:
        listing = files.subquery()
    else:
        files = files.where(~is_nested)
        folders = select(literal("folder").label("kind"),
                         (literal(prefix) + first_part).label("remote_path"),
                         literal(None).label("entry_id"), func.count().label("file_count"),
                         func.count().filter(pending).label("pending_count"),
                         func.count().filter(failed).label("failed_count")
                         ).where(*conditions, is_nested).group_by(first_part)
        listing = union_all(folders, files).subquery()
    total = await db.scalar(select(func.count()).select_from(listing))
    rows = (await db.execute(select(listing).order_by(
        case((listing.c.kind == "folder", 0), else_=1), listing.c.remote_path,
        listing.c.entry_id).offset((page - 1) * page_size).limit(page_size))).mappings().all()
    ids = [row["entry_id"] for row in rows if row["kind"] == "file"]
    records = {row.id: row for row in (await db.scalars(
        select(WebDAVEntry).where(WebDAVEntry.source_id == source.id,
                                 WebDAVEntry.id.in_(ids)))).all()} if ids else {}
    items = []
    for row in rows:
        if row["kind"] == "folder":
            items.append({"kind": "folder", "name": posixpath.basename(row["remote_path"]),
                          "remote_path": row["remote_path"], "file_count": row["file_count"],
                          "pending_count": row["pending_count"], "failed_count": row["failed_count"]})
        elif row["entry_id"] in records:
            items.append(entry_dict(records[row["entry_id"]]))
    return {"root": root, "directory": directory,
            "parent": None if directory == root else posixpath.dirname(directory),
            "page": page, "page_size": page_size, "total": total, "items": items,
            "pending_count": counts[0], "failed_count": counts[1]}
