from __future__ import annotations

import asyncio
from collections import namedtuple

from apps.api.api import system
from apps.api.core.db import get_db
from apps.api.main import app
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.processing import ProcessingJob
from apps.api.models.workspaces import Workspace


def _seed_failed_documents():
    async def seed():
        generator = app.dependency_overrides[get_db]()
        session = await anext(generator)
        try:
            research = Workspace(
                slug="research",
                name="研究空间",
                is_default=False,
                status="active",
                settings={},
            )
            session.add(research)
            await session.flush()
            for workspace_id, title in ((1, "默认失败资料"), (research.id, "研究失败资料")):
                document = Document(
                    workspace_id=workspace_id,
                    title=title,
                    source_type=DocumentSourceType.note,
                )
                session.add(document)
                await session.flush()
                version = DocumentVersion(
                    document_id=document.id,
                    version_number=1,
                    content_hash=f"failed-{document.id}",
                    processing_status="failed",
                )
                session.add(version)
                await session.flush()
                document.current_version_id = version.id
            await session.commit()
        finally:
            await generator.aclose()

    asyncio.run(seed())


def _build_processing_scenario(builder):
    """Drive ``builder(session)`` against the test database and commit."""

    async def runner():
        generator = app.dependency_overrides[get_db]()
        session = await anext(generator)
        try:
            await builder(session)
            await session.commit()
        finally:
            await generator.aclose()

    asyncio.run(runner())


def test_system_status_is_admin_only(client):
    test_client, _storage = client
    from apps.api.api.auth import require_admin
    from apps.api.main import app

    app.dependency_overrides.pop(require_admin)
    response = test_client.get("/api/system/status")
    assert response.status_code == 401


def test_system_status_reports_actionable_service_metrics(client, monkeypatch):
    test_client, _storage = client
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(system.shutil, "disk_usage", lambda _path: usage(1000, 250, 750))

    response = test_client.get("/api/system/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["uptime_seconds"] >= 0
    assert payload["database"]["status"] == "ok"
    assert payload["database"]["latency_ms"] >= 0
    assert payload["storage"] == {
        "status": "ok",
        "total_bytes": 1000,
        "used_bytes": 250,
        "free_bytes": 750,
        "used_percent": 25.0,
    }
    assert payload["processing"] == {
        "active": 0,
        "waiting": 0,
        "failed": 0,
        "failed_by_workspace": [],
    }


def test_system_status_attributes_failures_to_workspaces(client, monkeypatch):
    test_client, _storage = client
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(system.shutil, "disk_usage", lambda _path: usage(1000, 250, 750))
    _seed_failed_documents()

    response = test_client.get("/api/system/status")

    assert response.status_code == 200
    processing = response.json()["processing"]
    assert processing["failed"] == 2
    assert processing["failed_by_workspace"] == [
        {
            "workspace_id": 1,
            "workspace_slug": "default",
            "workspace_name": "默认空间",
            "workspace_status": "active",
            "failed": 1,
        },
        {
            "workspace_id": 2,
            "workspace_slug": "research",
            "workspace_name": "研究空间",
            "workspace_status": "active",
            "failed": 1,
        },
    ]


async def _make_version(
    session,
    *,
    document: Document,
    version_number: int,
    processing_status: str = "processing",
) -> DocumentVersion:
    version = DocumentVersion(
        document_id=document.id,
        version_number=version_number,
        content_hash=f"{document.id}-v{version_number}",
        processing_status=processing_status,
    )
    session.add(version)
    await session.flush()
    return version


async def _make_document(
    session,
    *,
    workspace_id: int,
    title: str,
    is_deleted: bool = False,
) -> Document:
    document = Document(
        workspace_id=workspace_id,
        title=title,
        source_type=DocumentSourceType.note,
        is_deleted=is_deleted,
    )
    session.add(document)
    await session.flush()
    return document


def _make_job(
    *,
    document: Document,
    version: DocumentVersion,
    stage: str,
    status: str,
    suffix: str = "",
) -> ProcessingJob:
    key = f"system-status:{version.id}:{stage}{suffix}"
    return ProcessingJob(
        document_id=document.id,
        document_version_id=version.id,
        stage=stage,
        status=status,
        idempotency_key=key,
        config_version="test",
    )


def test_system_status_ready_version_with_multiple_processing_jobs_counts_active_once(
    client, monkeypatch
):
    test_client, _storage = client
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(system.shutil, "disk_usage", lambda _path: usage(1000, 100, 900))

    async def seed(session):
        document = await _make_document(session, workspace_id=1, title="ready 文档")
        version = await _make_version(
            session, document=document, version_number=1, processing_status="ready"
        )
        document.current_version_id = version.id
        # Several vector sub-tasks plus a parsing job — should still count as
        # a single active document, never as a raw job count.
        for idx, stage in enumerate(
            ("embedding", "embedding", "embedding", "parsing")
        ):
            session.add(
                _make_job(
                    document=document,
                    version=version,
                    stage=stage,
                    status="processing",
                    suffix=f"-{idx}",
                )
            )

    _build_processing_scenario(seed)

    processing = test_client.get("/api/system/status").json()["processing"]
    assert processing == {
        "active": 1,
        "waiting": 0,
        "failed": 0,
        "failed_by_workspace": [],
    }


def test_system_status_only_created_or_retry_jobs_count_as_waiting(client, monkeypatch):
    test_client, _storage = client
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(system.shutil, "disk_usage", lambda _path: usage(1000, 100, 900))

    async def seed(session):
        document = await _make_document(session, workspace_id=1, title="排队文档")
        version = await _make_version(
            session, document=document, version_number=1, processing_status="created"
        )
        document.current_version_id = version.id
        session.add(
            _make_job(
                document=document,
                version=version,
                stage="parsing",
                status="created",
            )
        )
        session.add(
            _make_job(
                document=document,
                version=version,
                stage="chunking",
                status="retry",
                suffix="-retry",
            )
        )

    _build_processing_scenario(seed)

    processing = test_client.get("/api/system/status").json()["processing"]
    assert processing == {
        "active": 0,
        "waiting": 1,
        "failed": 0,
        "failed_by_workspace": [],
    }


def test_system_status_processing_beats_created_and_retry_per_document(client, monkeypatch):
    test_client, _storage = client
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(system.shutil, "disk_usage", lambda _path: usage(1000, 100, 900))

    async def seed(session):
        for suffix, stages in (
            ("alpha", (("embedding", "processing"), ("parsing", "created"))),
            ("beta", (("embedding", "created"), ("parsing", "retry"))),
        ):
            document = await _make_document(
                session, workspace_id=1, title=f"文档-{suffix}"
            )
            version = await _make_version(
                session,
                document=document,
                version_number=1,
                processing_status="processing",
            )
            document.current_version_id = version.id
            for idx, (stage, status) in enumerate(stages):
                session.add(
                    _make_job(
                        document=document,
                        version=version,
                        stage=stage,
                        status=status,
                        suffix=f"-{idx}",
                    )
                )

    _build_processing_scenario(seed)

    processing = test_client.get("/api/system/status").json()["processing"]
    # alpha has a ``processing`` job, beta has only ``created`` / ``retry``.
    # Each document is counted exactly once, with ``processing`` taking
    # precedence over the queueing statuses.
    assert processing["active"] == 1
    assert processing["waiting"] == 1
    assert processing["failed"] == 0


def test_system_status_ignores_jobs_for_old_versions_and_deleted_documents(
    client, monkeypatch
):
    test_client, _storage = client
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(system.shutil, "disk_usage", lambda _path: usage(1000, 100, 900))

    async def seed(session):
        live = await _make_document(session, workspace_id=1, title="当前版本")
        live_v2 = await _make_version(
            session, document=live, version_number=2, processing_status="ready"
        )
        live.current_version_id = live_v2.id
        # No live jobs for this document — should not affect counters.

        archived = await _make_document(session, workspace_id=1, title="旧版本文档")
        archived_v1 = await _make_version(
            session, document=archived, version_number=1, processing_status="processing"
        )
        archived_v2 = await _make_version(
            session, document=archived, version_number=2, processing_status="ready"
        )
        archived.current_version_id = archived_v2.id
        # Job row referencing the now-superseded v1 must not bubble up.
        session.add(
            _make_job(
                document=archived,
                version=archived_v1,
                stage="embedding",
                status="processing",
                suffix="-stale",
            )
        )

        deleted = await _make_document(
            session,
            workspace_id=1,
            title="已删除文档",
            is_deleted=True,
        )
        deleted_version = await _make_version(
            session,
            document=deleted,
            version_number=1,
            processing_status="processing",
        )
        deleted.current_version_id = deleted_version.id
        # Job row for a soft-deleted document must not bubble up either.
        session.add(
            _make_job(
                document=deleted,
                version=deleted_version,
                stage="embedding",
                status="processing",
                suffix="-deleted",
            )
        )

    _build_processing_scenario(seed)

    processing = test_client.get("/api/system/status").json()["processing"]
    assert processing["active"] == 0
    assert processing["waiting"] == 0
    assert processing["failed"] == 0


def test_system_status_falls_back_to_legacy_processing_status(client, monkeypatch):
    test_client, _storage = client
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(system.shutil, "disk_usage", lambda _path: usage(1000, 100, 900))

    async def seed(session):
        # Version 1 still reports a running status (no surviving ProcessingJob
        # row) — backwards-compatible behaviour should keep flagging it.
        legacy = await _make_document(session, workspace_id=1, title="遗留处理中")
        legacy_v1 = await _make_version(
            session, document=legacy, version_number=1, processing_status="processing"
        )
        legacy.current_version_id = legacy_v1.id

        # Version 2 reports ``created`` only via DocumentVersion.processing_status.
        seeded = await _make_document(session, workspace_id=1, title="遗留排队")
        seeded_v1 = await _make_version(
            session, document=seeded, version_number=1, processing_status="created"
        )
        seeded.current_version_id = seeded_v1.id

        # A fully terminal version must not be counted.
        done = await _make_document(session, workspace_id=1, title="已完成")
        done_v1 = await _make_version(
            session, document=done, version_number=1, processing_status="ready"
        )
        done.current_version_id = done_v1.id

    _build_processing_scenario(seed)

    processing = test_client.get("/api/system/status").json()["processing"]
    assert processing["active"] == 1
    assert processing["waiting"] == 1
    assert processing["failed"] == 0


def test_system_status_failed_version_beats_stale_processing_job(client, monkeypatch):
    test_client, _storage = client
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(system.shutil, "disk_usage", lambda _path: usage(1000, 100, 900))

    async def seed(session):
        document = await _make_document(session, workspace_id=1, title="失败后残留任务")
        version = await _make_version(
            session, document=document, version_number=1, processing_status="failed"
        )
        document.current_version_id = version.id
        session.add(
            _make_job(
                document=document,
                version=version,
                stage="embedding",
                status="processing",
                suffix="-stale",
            )
        )

    _build_processing_scenario(seed)

    processing = test_client.get("/api/system/status").json()["processing"]
    assert processing["active"] == 0
    assert processing["waiting"] == 0
    assert processing["failed"] == 1
