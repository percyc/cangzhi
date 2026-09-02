"""Authenticated operational status for the self-hosted Cangzhi service."""

from __future__ import annotations

import shutil
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.db import get_db
from ..models.documents import Document, DocumentVersion
from ..models.processing import ProcessingJob
from ..models.workspaces import Workspace

router = APIRouter()
_STARTED_AT = time.monotonic()

# Processing job statuses that mean the queue is doing something.
_RUNNING_JOB_STATUSES = ("processing", "created", "retry")


async def _live_job_statuses(db: AsyncSession) -> list[tuple[int, str]]:
    """Return distinct ``(document_version_id, status)`` live-job pairs.

    Only the document versions that are still the document's ``current_version``
    and whose parent document is not soft-deleted are considered. Tasks
    attached to older versions are dropped so a backfill does not keep
    flagging the document as busy after a new version was published. Distinct
    status pairs keep large embedding builds from returning one row per chunk.
    """

    rows = (
        await db.execute(
            select(ProcessingJob.document_version_id, ProcessingJob.status)
            .join(
                DocumentVersion,
                DocumentVersion.id == ProcessingJob.document_version_id,
            )
            .join(
                Document,
                Document.id == DocumentVersion.document_id,
            )
            .where(
                Document.is_deleted.is_(False),
                Document.current_version_id == DocumentVersion.id,
                ProcessingJob.status.in_(_RUNNING_JOB_STATUSES),
            )
            .distinct()
            .execution_options(include_all_workspaces=True)
        )
    ).all()
    return [(int(version_id), str(status)) for version_id, status in rows]


@router.get("/system/status")
async def system_status(db: AsyncSession = Depends(get_db)):
    """Return actionable application health without exposing host internals."""

    database_started = time.perf_counter()
    await db.execute(text("SELECT 1"))
    database_latency_ms = round((time.perf_counter() - database_started) * 1000, 1)

    # Live processing jobs limited to current, non-deleted document versions.
    # The job row count is collapsed per (document_version_id) so multiple
    # vector sub-tasks count as a single busy document; the row count itself
    # is intentionally not surfaced to callers.
    job_status_by_version: dict[int, set[str]] = {}
    for version_id, status in await _live_job_statuses(db):
        job_status_by_version.setdefault(version_id, set()).add(status)

    # Backwards-compatible view: the historical implementation counted
    # DocumentVersion.processing_status directly. Keep that signal in the mix
    # so documents whose worker has not yet created a ProcessingJob row (for
    # example immediately after a re-enqueue) still surface.
    version_status_rows = (
        await db.execute(
            select(DocumentVersion.id, DocumentVersion.processing_status)
            .join(Document, Document.current_version_id == DocumentVersion.id)
            .where(Document.is_deleted.is_(False))
            .execution_options(include_all_workspaces=True)
        )
    ).all()
    version_status_by_id: dict[int, str] = {
        version_id: str(status) for version_id, status in version_status_rows
    }

    active = 0
    waiting = 0
    for version_id, version_status in version_status_by_id.items():
        # A failed current version is authoritative. A worker crash can leave
        # a stale processing job behind, which must not make one document look
        # both active and failed.
        if version_status == "failed":
            continue
        statuses = job_status_by_version.get(version_id, set())
        if "processing" in statuses:
            active += 1
            continue
        if statuses & {"created", "retry"}:
            waiting += 1
            continue
        # Include versions that report a running status but have no live job
        # row yet, preserving the historical endpoint behavior during enqueue.
        if version_status == "processing":
            active += 1
        elif version_status in {"created", "retry"}:
            waiting += 1

    failed = (
        await db.execute(
            select(func.count(Document.id))
            .join(
                DocumentVersion, Document.current_version_id == DocumentVersion.id
            )
            .where(
                Document.is_deleted.is_(False),
                DocumentVersion.processing_status == "failed",
            )
            .execution_options(include_all_workspaces=True)
        )
    ).scalar_one()

    failed_workspace_rows = (
        await db.execute(
            select(
                Workspace.id,
                Workspace.slug,
                Workspace.name,
                Workspace.status,
                func.count(Document.id).label("failed_count"),
            )
            .select_from(Document)
            .join(DocumentVersion, Document.current_version_id == DocumentVersion.id)
            .join(Workspace, Workspace.id == Document.workspace_id)
            .where(
                Document.is_deleted.is_(False),
                DocumentVersion.processing_status == "failed",
            )
            .group_by(Workspace.id, Workspace.slug, Workspace.name, Workspace.status)
            .order_by(Workspace.is_default.desc(), Workspace.name, Workspace.id)
            .execution_options(include_all_workspaces=True)
        )
    ).all()
    failed_by_workspace = [
        {
            "workspace_id": workspace_id,
            "workspace_slug": slug,
            "workspace_name": name,
            "workspace_status": workspace_status,
            "failed": int(failed_count),
        }
        for workspace_id, slug, name, workspace_status, failed_count in failed_workspace_rows
    ]

    capacity = shutil.disk_usage(settings.storage_path)
    used_percent = round((capacity.used / capacity.total) * 100, 1) if capacity.total else 0.0
    overall = "degraded" if failed > 0 or used_percent >= 90 else "ok"

    return {
        "status": overall,
        "sampled_at": datetime.now(timezone.utc).isoformat(),
        "uptime_seconds": int(time.monotonic() - _STARTED_AT),
        "database": {"status": "ok", "latency_ms": database_latency_ms},
        "storage": {
            "status": "warning" if used_percent >= 90 else "ok",
            "total_bytes": capacity.total,
            "used_bytes": capacity.used,
            "free_bytes": capacity.free,
            "used_percent": used_percent,
        },
        "processing": {
            "active": active,
            "waiting": waiting,
            "failed": failed,
            "failed_by_workspace": failed_by_workspace,
        },
    }
