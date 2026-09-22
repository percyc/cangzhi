"""Freshness metadata and durable refresh scheduling for database snapshots."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.database_source import DatabaseSnapshot, DatabaseSource
from ..models.datasets import KnowledgeDataset
from ..models.processing import ProcessingJob

FRESHNESS_MODES = frozenset({"manual", "background", "strict"})
DATABASE_REFRESH_STAGE = "database_refresh"
MIN_REFRESH_MINUTES = 5
MAX_REFRESH_MINUTES = 43_200
BACKGROUND_REFRESH_DELAY_SECONDS = 60


def validate_freshness_policy(mode: str, interval_minutes: int) -> tuple[str, int]:
    normalized = str(mode or "").strip().lower()
    if normalized not in FRESHNESS_MODES:
        raise ValueError("刷新策略必须是 manual、background 或 strict")
    interval = int(interval_minutes)
    if interval < MIN_REFRESH_MINUTES or interval > MAX_REFRESH_MINUTES:
        raise ValueError("刷新间隔必须在 5 分钟到 30 天之间")
    return normalized, interval


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def get_dataset_freshness(
    db: AsyncSession, dataset: KnowledgeDataset
) -> dict[str, Any]:
    return (await get_datasets_freshness(db, [dataset]))[dataset.id]


async def get_datasets_freshness(
    db: AsyncSession, datasets: list[KnowledgeDataset]
) -> dict[int, dict[str, Any]]:
    """Resolve source freshness in two queries regardless of catalog size."""

    if not datasets:
        return {}
    dataset_ids = [item.id for item in datasets]
    rows = list(
        (
            await db.execute(
                select(DatabaseSnapshot, DatabaseSource)
                .join(
                    DatabaseSource,
                    DatabaseSource.id == DatabaseSnapshot.source_id,
                )
                .where(DatabaseSnapshot.dataset_id.in_(dataset_ids))
            )
        ).all()
    )
    pending_document_ids = set(
        (
            await db.scalars(
                select(ProcessingJob.document_id).where(
                    ProcessingJob.document_id.in_(
                        [snapshot.document_id for snapshot, _source in rows]
                    ),
                    ProcessingJob.stage == DATABASE_REFRESH_STAGE,
                    ProcessingJob.status.in_(("created", "retry", "processing")),
                )
            )
        ).all()
        if rows
        else []
    )
    now = datetime.now(timezone.utc)
    output = {
        item.id: {
            "kind": "local",
            "refresh_mode": "not_applicable",
            "snapshot_at": None,
            "age_seconds": None,
            "stale": False,
            "refresh_pending": False,
        }
        for item in datasets
    }
    versions = {item.id: item.document_version_id for item in datasets}
    for snapshot, source in rows:
        snapshot_at = _utc(snapshot.snapshot_at)
        age_seconds = (
            max(0, int((now - snapshot_at).total_seconds()))
            if snapshot_at
            else None
        )
        interval_seconds = int(source.freshness_interval_minutes) * 60
        stale = snapshot_at is None or bool(
            age_seconds is not None and age_seconds >= interval_seconds
        )
        output[snapshot.dataset_id] = {
            "kind": "database_snapshot",
            "source_id": source.id,
            "source_name": source.name,
            "engine": source.engine,
            "schema_name": snapshot.schema_name,
            "table_name": snapshot.table_name,
            "snapshot_at": snapshot_at.isoformat() if snapshot_at else None,
            "age_seconds": age_seconds,
            "refresh_mode": source.freshness_mode,
            "refresh_interval_minutes": source.freshness_interval_minutes,
            "stale": stale,
            "refresh_pending": snapshot.document_id in pending_document_ids,
            "last_error": snapshot.last_error or source.last_error,
            "data_version": versions[snapshot.dataset_id],
            "artifact_fixed_for_query": True,
        }
    return output


async def schedule_dataset_refresh(
    db: AsyncSession,
    dataset: KnowledgeDataset,
    freshness: dict[str, Any],
    *,
    immediate: bool = False,
) -> bool:
    if (
        freshness.get("kind") != "database_snapshot"
        or not freshness.get("stale")
        or freshness.get("refresh_mode") == "manual"
        or freshness.get("refresh_pending")
    ):
        return False
    snapshot = await db.scalar(
        select(DatabaseSnapshot).where(DatabaseSnapshot.dataset_id == dataset.id)
    )
    if snapshot is None:
        return False
    source = await db.get(DatabaseSource, snapshot.source_id)
    if source is None or not source.is_enabled:
        return False
    stamp = int(_utc(snapshot.snapshot_at).timestamp()) if snapshot.snapshot_at else 0
    key = f"db-refresh:{snapshot.id}:{stamp}"
    existing = await db.scalar(
        select(ProcessingJob).where(ProcessingJob.idempotency_key == key)
    )
    if existing is not None:
        if existing.status in {"created", "retry", "processing"}:
            return True
        existing.status = "created"
        existing.retry_count = 0
        existing.next_retry_at = (
            None
            if immediate
            else datetime.now(timezone.utc)
            + timedelta(seconds=BACKGROUND_REFRESH_DELAY_SECONDS)
        )
        existing.last_error = None
        existing.error_details = None
        existing.started_at = None
        existing.finished_at = None
        db.add(existing)
        await db.commit()
        freshness["refresh_pending"] = True
        return True
    db.add(
        ProcessingJob(
            document_id=snapshot.document_id,
            document_version_id=dataset.document_version_id,
            stage=DATABASE_REFRESH_STAGE,
            status="created",
            idempotency_key=key,
            retry_count=0,
            max_retries=3,
            next_retry_at=(
                None
                if immediate
                else datetime.now(timezone.utc)
                + timedelta(seconds=BACKGROUND_REFRESH_DELAY_SECONDS)
            ),
            config_version=f"database-refresh:{snapshot.id}",
        )
    )
    try:
        await db.commit()
    except IntegrityError:
        # Concurrent schema/query calls may observe the same stale snapshot.
        # The unique idempotency key is the final arbiter; the losing caller
        # joins the already scheduled refresh instead of surfacing a 500.
        await db.rollback()
        if await db.scalar(
            select(ProcessingJob.id).where(ProcessingJob.idempotency_key == key)
        ):
            freshness["refresh_pending"] = True
            return True
        raise
    freshness["refresh_pending"] = True
    return True


__all__ = [
    "DATABASE_REFRESH_STAGE",
    "FRESHNESS_MODES",
    "get_dataset_freshness",
    "get_datasets_freshness",
    "schedule_dataset_refresh",
    "validate_freshness_policy",
]
