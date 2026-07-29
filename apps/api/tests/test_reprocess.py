import asyncio

from sqlalchemy import select

from apps.api.core.db import get_db
from apps.api.main import app
from apps.api.models.documents import Document, DocumentVersion
from apps.api.models.processing import ProcessingJob


def test_reprocess_failed_document_is_idempotent(client):
    test_client, _ = client
    created = test_client.post(
        "/api/notes",
        json={"title": "重试测试", "content": "需要重新处理的内容"},
    ).json()
    document_id = created["id"]
    version_id = created["current_version"]["id"]
    db_dependency = app.dependency_overrides[get_db]

    async def mark_failed():
        dependency = db_dependency()
        session = await anext(dependency)
        try:
            document = await session.get(Document, document_id)
            version = await session.get(DocumentVersion, document.current_version_id)
            version.processing_status = "failed"
            await session.commit()
        finally:
            await dependency.aclose()

    asyncio.run(mark_failed())

    first = test_client.post(f"/api/documents/{document_id}/reprocess")
    second = test_client.post(f"/api/documents/{document_id}/reprocess")
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["job_id"] == second.json()["job_id"]

    async def count_reprocess_jobs():
        dependency = db_dependency()
        session = await anext(dependency)
        try:
            result = await session.execute(
                select(ProcessingJob).where(
                    ProcessingJob.idempotency_key
                    == f"{document_id}:{version_id}:parsing:v1"
                )
            )
            return len(result.scalars().all())
        finally:
            await dependency.aclose()

    assert asyncio.run(count_reprocess_jobs()) == 1


def test_reprocess_nonexistent_document(client):
    test_client, _ = client
    response = test_client.post("/api/documents/9999/reprocess")
    assert response.status_code == 404
    assert response.json()["detail"] == "资料不存在"


def test_reprocess_url_discards_old_snapshot(client):
    test_client, _ = client
    created = test_client.post(
        "/api/sources/url",
        json={"url": "https://example.com/article"},
    ).json()
    document_id = created["id"]
    db_dependency = app.dependency_overrides[get_db]

    async def mark_snapshot():
        dependency = db_dependency()
        session = await anext(dependency)
        try:
            document = await session.get(Document, document_id)
            version = await session.get(DocumentVersion, document.current_version_id)
            version.blob_id = None
            version.content_hash = "old-block-page"
            version.processing_status = "ready"
            await session.commit()
        finally:
            await dependency.aclose()

    asyncio.run(mark_snapshot())
    response = test_client.post(f"/api/documents/{document_id}/reprocess")
    assert response.status_code == 200

    async def load_version():
        dependency = db_dependency()
        session = await anext(dependency)
        try:
            document = await session.get(Document, document_id)
            return await session.get(DocumentVersion, document.current_version_id)
        finally:
            await dependency.aclose()

    version = asyncio.run(load_version())
    assert version.blob_id is None
    assert version.content_hash == ""


def test_latest_job_returns_initial_job(client):
    test_client, _ = client
    created = test_client.post(
        "/api/notes",
        json={"title": "任务状态", "content": "测试任务状态"},
    ).json()
    response = test_client.get(f"/api/documents/{created['id']}/latest-job")
    assert response.status_code == 200
    assert response.json()["status"] == "created"
