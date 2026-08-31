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
from ..models.workspaces import Workspace

router = APIRouter()
_STARTED_AT = time.monotonic()


@router.get("/system/status")
async def system_status(db: AsyncSession = Depends(get_db)):
    """Return actionable application health without exposing host internals."""

    database_started = time.perf_counter()
    await db.execute(text("SELECT 1"))
    database_latency_ms = round((time.perf_counter() - database_started) * 1000, 1)

    status_rows = (
        await db.execute(
            select(DocumentVersion.processing_status, func.count(DocumentVersion.id))
            .join(Document, Document.current_version_id == DocumentVersion.id)
            .where(Document.is_deleted.is_(False))
            .group_by(DocumentVersion.processing_status)
            .execution_options(include_all_workspaces=True)
        )
    ).all()
    counts = {str(status): int(count) for status, count in status_rows}
    waiting = sum(counts.get(status, 0) for status in ("created", "retry"))
    active = counts.get("processing", 0)
    failed = counts.get("failed", 0)

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
