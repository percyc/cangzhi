"""Recovery must not broaden a failed stage into a destructive full reparse."""

import asyncio

import pytest
from sqlalchemy import select

from apps.api.core.db import get_db
from apps.api.main import app
from apps.api.models.blobs import Blob
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.processing import ProcessingJob
from apps.api.models.taxonomy import DocumentSummary
from apps.api.models.workspaces import Workspace


def seed(stages, *, structured=True, workspace_id=1):
    async def run():
        async for db in app.dependency_overrides[get_db]():
            blob = Blob(sha256="a" * 64, storage_key="synthetic-snapshot", content_type="text/html", file_size=10)
            db.add(blob)
            await db.flush()
            document = Document(title="Synthetic page", source_type=DocumentSourceType.url,
                                source_url="https://example.invalid/synthetic", workspace_id=workspace_id)
            db.add(document)
            await db.flush()
            version = DocumentVersion(document_id=document.id, version_number=1, blob_id=blob.id,
                content_hash="original-hash", raw_content="Original synthetic facts",
                structured_content={"blocks": [{"type": "paragraph", "text": "Original synthetic facts"}]} if structured else None,
                processing_status="failed")
            db.add(version)
            await db.flush()
            document.current_version_id = version.id
            db.add(DocumentSummary(document_id=document.id, document_version_id=version.id, summary="Human summary", source="manual"))
            for index, (stage, status) in enumerate(stages):
                db.add(ProcessingJob(document_id=document.id, document_version_id=version.id,
                    stage=stage, status=status, idempotency_key=f"synthetic:{index}",
                    config_version="keep-strategy", retry_count=3, last_error="synthetic failure"))
            await db.commit()
            return document.id, version.id, blob.id
    return asyncio.run(run())


def snapshot(version_id):
    async def run():
        async for db in app.dependency_overrides[get_db]():
            version = await db.get(DocumentVersion, version_id)
            jobs = (await db.scalars(select(ProcessingJob).order_by(ProcessingJob.id))).all()
            return {
                "blob_id": version.blob_id, "hash": version.content_hash,
                "raw": version.raw_content, "structured": version.structured_content,
                "status": version.processing_status,
                "summary": await db.scalar(select(DocumentSummary.summary)),
                "jobs": [(job.stage, job.status, job.retry_count, job.config_version, job.last_error) for job in jobs],
            }
    return asyncio.run(run())


@pytest.mark.parametrize("failed_stage", ["understanding", "chunking"])
def test_retry_only_failed_stage_preserves_url_snapshot_and_organization(client, failed_stage):
    http, _ = client
    document_id, version_id, blob_id = seed([
        ("parsing", "completed"), (failed_stage, "failed"), ("dataset_semantics", "completed")])
    before = snapshot(version_id)
    response = http.post(f"/api/documents/{document_id}/retry-failed")
    assert response.status_code == 200
    assert response.json()["stages"] == [failed_stage]
    after = snapshot(version_id)
    assert after["blob_id"] == blob_id
    for key in ("hash", "raw", "structured", "summary"):
        assert after[key] == before[key]
    assert after["status"] == "ready"
    assert after["jobs"][0] == before["jobs"][0]
    assert after["jobs"][1] == (failed_stage, "created", 0, "keep-strategy", None)
    assert after["jobs"][2] == before["jobs"][2]
    again = http.post(f"/api/documents/{document_id}/retry-failed")
    assert again.status_code == 200 and again.json()["job_ids"] == []
    assert snapshot(version_id) == after


@pytest.mark.parametrize("parse_stage", ["stored", "parsing"])
def test_retry_parsing_prerequisite_first_without_discarding_saved_blob(client, parse_stage):
    http, _ = client
    document_id, version_id, blob_id = seed([(parse_stage, "failed"), ("chunking", "failed")])
    response = http.post(f"/api/documents/{document_id}/retry-failed")
    assert response.status_code == 200 and response.json()["stages"] == ["parsing"]
    state = snapshot(version_id)
    assert state["blob_id"] == blob_id and state["hash"] == "original-hash"
    assert state["status"] == "created"
    assert state["jobs"][0][1] == "created"
    assert state["jobs"][1][1] == "failed"


@pytest.mark.parametrize("running_stage", ["parsing", "embedding", "understanding"])
def test_retry_does_not_reset_any_running_task(client, running_stage):
    http, _ = client
    document_id, version_id, _ = seed([("chunking", "failed"), (running_stage, "processing")])
    before = snapshot(version_id)
    assert http.post(f"/api/documents/{document_id}/retry-failed").status_code == 409
    assert snapshot(version_id) == before


def test_retry_ignores_obsolete_failed_stage_and_preserves_current_strategy(client):
    http, _ = client
    document_id, version_id, _ = seed([("chunking", "failed"), ("chunking", "completed")])
    before = snapshot(version_id)
    assert http.post(f"/api/documents/{document_id}/retry-failed").json()["job_ids"] == []
    assert snapshot(version_id) == before


def test_retry_missing_parsed_content_requires_explicit_full_reprocess(client):
    http, _ = client
    document_id, version_id, _ = seed([("understanding", "failed")], structured=False)
    before = snapshot(version_id)
    assert http.post(f"/api/documents/{document_id}/retry-failed").status_code == 409
    assert snapshot(version_id) == before


def test_retry_base_stage_does_not_reset_failed_vector_or_leave_terminal_version(client):
    http, _ = client
    document_id, version_id, _ = seed([("parsing", "completed"), ("chunking", "failed"), ("embedding", "failed")])
    before = snapshot(version_id)
    assert http.post(f"/api/documents/{document_id}/retry-failed").json()["stages"] == ["chunking"]
    after = snapshot(version_id)
    assert after["status"] == "ready"
    assert after["jobs"][2] == before["jobs"][2]


def test_retry_does_not_start_downstream_while_parse_is_queued(client):
    http, _ = client
    document_id, version_id, _ = seed([("parsing", "retry"), ("understanding", "failed")])
    before = snapshot(version_id)
    assert http.post(f"/api/documents/{document_id}/retry-failed").json()["job_ids"] == []
    assert snapshot(version_id) == before


def test_retry_full_reprocess_failure_requires_explicit_confirmation(client):
    http, _ = client
    document_id, version_id, _ = seed([("parsing", "failed")])
    async def mark_full_reprocess():
        async for db in app.dependency_overrides[get_db]():
            job = await db.scalar(select(ProcessingJob))
            job.config_version = "2"
            await db.commit()
    asyncio.run(mark_full_reprocess())
    before = snapshot(version_id)
    response = http.post(f"/api/documents/{document_id}/retry-failed")
    assert response.status_code == 409
    assert "完整重新处理" in response.json()["detail"]
    assert snapshot(version_id) == before


def test_retry_enforces_workspace_and_authentication(client):
    http, _ = client
    async def create_space():
        async for db in app.dependency_overrides[get_db]():
            space = Workspace(slug="isolated", name="Synthetic isolated", settings={})
            db.add(space)
            await db.commit()
            return space.id
    document_id, version_id, _ = seed([("chunking", "failed")], workspace_id=asyncio.run(create_space()))
    assert http.post(f"/api/documents/{document_id}/retry-failed").status_code == 404
    assert http.post(f"/api/documents/{document_id}/retry-failed", headers={"X-Cangzhi-Workspace": "isolated"}).status_code == 200
    from apps.api.api.auth import require_admin
    app.dependency_overrides.pop(require_admin)
    assert http.post(f"/api/documents/{document_id}/retry-failed").status_code == 401
