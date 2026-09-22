from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from apps.api.core.db import get_db
from apps.api.main import app
from apps.api.models.database_source import DatabaseSnapshot, DatabaseSource
from apps.api.models.datasets import KnowledgeDataset
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.processing import ProcessingJob
from apps.api.services.dataset_freshness import (
    DATABASE_REFRESH_STAGE,
    get_dataset_freshness,
    schedule_dataset_refresh,
    validate_freshness_policy,
)


def test_validate_freshness_policy():
    assert validate_freshness_policy("background", 60) == ("background", 60)
    for mode, interval in (("live", 60), ("manual", 4), ("strict", 43_201)):
        try:
            validate_freshness_policy(mode, interval)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid freshness policy was accepted")


def test_database_snapshot_freshness_and_refresh_job_are_deduplicated(client):
    _test_client, _storage = client

    async def run():
        generator = app.dependency_overrides[get_db]()
        db = await anext(generator)
        try:
            source = DatabaseSource(
                workspace_id=1,
                name="reporting",
                engine="mysql",
                host="db.example.com",
                port=3306,
                database_name="reports",
                username="reader",
                password_cipher="cipher",
                has_password=True,
                ssl_mode="required",
                trusted_private_network=False,
                is_enabled=True,
                status="idle",
                freshness_mode="background",
                freshness_interval_minutes=60,
            )
            db.add(source)
            await db.flush()
            document = Document(
                workspace_id=1,
                title="reporting.sales",
                source_type=DocumentSourceType.file,
            )
            db.add(document)
            await db.flush()
            version = DocumentVersion(
                document_id=document.id,
                version_number=1,
                content_hash="freshness",
                processing_status="ready",
            )
            db.add(version)
            await db.flush()
            document.current_version_id = version.id
            dataset = KnowledgeDataset(
                document_id=document.id,
                document_version_id=version.id,
                name="sales",
                sheet_name="sales",
                region_index=1,
                status="ready",
                row_count=3,
                column_count=2,
                profile={},
            )
            db.add(dataset)
            await db.flush()
            db.add(
                DatabaseSnapshot(
                    workspace_id=1,
                    source_id=source.id,
                    schema_name="reports",
                    table_name="sales",
                    document_id=document.id,
                    dataset_id=dataset.id,
                    row_count=3,
                    snapshot_fingerprint="abc",
                    snapshot_at=datetime.now(timezone.utc) - timedelta(hours=2),
                )
            )
            await db.commit()

            freshness = await get_dataset_freshness(db, dataset)
            first = await schedule_dataset_refresh(db, dataset, freshness)
            second = await schedule_dataset_refresh(db, dataset, freshness)
            count = await db.scalar(
                select(func.count(ProcessingJob.id)).where(
                    ProcessingJob.stage == DATABASE_REFRESH_STAGE
                )
            )
            job = await db.scalar(
                select(ProcessingJob).where(
                    ProcessingJob.stage == DATABASE_REFRESH_STAGE
                )
            )
            return freshness, first, second, count, job
        finally:
            await generator.aclose()

    freshness, first, second, count, job = asyncio.run(run())
    assert freshness["kind"] == "database_snapshot"
    assert freshness["stale"] is True
    assert freshness["refresh_pending"] is True
    assert freshness["data_version"] > 0
    assert first is True
    assert second is False
    assert count == 1
    assert job is not None and job.next_retry_at is not None


def test_local_dataset_freshness_is_not_applicable(client):
    _test_client, _storage = client

    async def run():
        generator = app.dependency_overrides[get_db]()
        db = await anext(generator)
        try:
            document = Document(title="local", source_type=DocumentSourceType.file)
            db.add(document)
            await db.flush()
            version = DocumentVersion(
                document_id=document.id,
                version_number=1,
                content_hash="local",
                processing_status="ready",
            )
            db.add(version)
            await db.flush()
            document.current_version_id = version.id
            dataset = KnowledgeDataset(
                document_id=document.id,
                document_version_id=version.id,
                name="local",
                sheet_name="local",
                region_index=1,
                status="ready",
                row_count=0,
                column_count=0,
                profile={},
            )
            db.add(dataset)
            await db.commit()
            return await get_dataset_freshness(db, dataset)
        finally:
            await generator.aclose()

    assert asyncio.run(run())["kind"] == "local"
