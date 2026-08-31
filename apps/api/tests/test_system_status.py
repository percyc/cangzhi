from __future__ import annotations

import asyncio
from collections import namedtuple

from apps.api.api import system
from apps.api.core.db import get_db
from apps.api.main import app
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
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
