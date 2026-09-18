"""End-to-end and regression checks independent of the scheduler implementation."""
from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import apps.api.models
from apps.api.core.db import Base
from apps.api.models.documents import Document, DocumentVersion, DocumentSourceType
from apps.api.models.processing import ProcessingJob
from apps.api.models.chunks import DocumentChunk
from apps.api.models.workspaces import Workspace
from apps.worker import priority_maintenance as priority
from apps.worker.services.processor import ASSISTED_CHUNKING_CONFIG

CUTOFF = datetime(2026, 9, 18, tzinfo=timezone.utc)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Workspace(id=1, slug="default", name="Test", status="active"))
        session.commit()
        yield session
    engine.dispose()


def document(db, *, fresh=True, stage="parsing", assisted=False):
    doc = Document(workspace_id=1, title="test", source_type=DocumentSourceType.note)
    db.add(doc)
    db.flush()
    version = DocumentVersion(document_id=doc.id, version_number=1,
        content_hash=uuid.uuid4().hex, processing_status="created",
        created_at=CUTOFF + timedelta(days=1 if fresh else -1),
        raw_content="# 新上传\n新文件应优先解析，并能生成可检索正文。",
        meta={"chunk_config_version": ASSISTED_CHUNKING_CONFIG} if assisted else {})
    db.add(version)
    db.flush()
    doc.current_version_id = version.id
    job = ProcessingJob(document_id=doc.id, document_version_id=version.id,
        idempotency_key=uuid.uuid4().hex, config_version=ASSISTED_CHUNKING_CONFIG if assisted else "1",
        stage=stage, status="created")
    db.add(job)
    db.commit()
    return doc, version, job


def test_new_document_real_parse_and_chunk_old_queue_untouched(db, monkeypatch):
    old, old_version, old_job = document(db, fresh=False)
    new, version, job = document(db)
    monkeypatch.setattr("apps.worker.services.processor.build_provider_from_session", lambda _: None)
    priority.reset_shutdown_flag()
    state = {"fg_cursor": 0, "fg_weight": 0}
    for _ in range(8):
        priority.run_priority_iteration(db, cutoff=CUTOFF, history_ids=[], state=state)
    db.refresh(version)
    db.refresh(old_job)
    assert version.processing_status == "ready"
    assert version.structured_content
    assert db.scalar(select(DocumentChunk.id).where(DocumentChunk.document_version_id == version.id).limit(1))
    assert old_job.status == "created"
    assert old_version.structured_content is None


def test_history_ids_are_document_ids_and_never_stale_versions(db):
    doc, version, job = document(db, fresh=False, stage="embedding", assisted=True)
    # Deliberately make the job PK different from the document PK.
    job.id = 12345
    db.commit()
    assert priority._claim_one_background_job(db, [doc.id]) == 12345
    job.status = "created"
    doc.current_version_id = None
    db.commit()
    assert priority._claim_one_background_job(db, [doc.id]) is None


def test_foreground_scans_empty_stages_before_history(db, monkeypatch):
    old, _, _ = document(db, fresh=False, stage="embedding", assisted=True)
    new, _, new_job = document(db, stage="embedding")
    called = []
    def done(session, job_id):
        called.append(job_id)
        session.get(ProcessingJob, job_id).status = "completed"
        session.commit()
        return True
    monkeypatch.setattr(priority, "process_single_job", done)
    priority.reset_shutdown_flag()
    priority.run_priority_iteration(db, cutoff=CUTOFF, history_ids=[old.id], state={"fg_cursor": 0, "fg_weight": 0})
    assert called[0] == new_job.id


def test_shutdown_checked_between_jobs(db, monkeypatch):
    document(db)
    document(db, stage="chunking")
    calls = []
    def shutdown(session, job_id):
        calls.append(job_id)
        session.get(ProcessingJob, job_id).status = "completed"
        session.commit()
        priority._request_shutdown()
        return True
    monkeypatch.setattr(priority, "process_single_job", shutdown)
    priority.reset_shutdown_flag()
    try:
        priority.run_priority_iteration(db, cutoff=CUTOFF, history_ids=[], state={"fg_cursor": 0, "fg_weight": 0})
        assert len(calls) == 1
    finally:
        priority.reset_shutdown_flag()


def test_oversize_pdf_uses_rules_instead_of_failing_new_upload(db):
    doc, version, job = document(db, stage="chunking")
    version.structured_content = {"document_type": "pdf", "blocks": [{"type": "paragraph", "text": "line"}] * (priority.MAX_BLOCKS + 1)}
    db.commit()
    original = version.structured_content
    assert priority._claim_one_foreground_job(db, CUTOFF, stage="chunking") == job.id
    assert job.config_version == "1"
    assert version.structured_content == original
    assert version.meta["chunking_policy_fallback"]["reason"] == "assisted_block_limit"


def test_executor_exception_does_not_leave_permanent_processing(db, monkeypatch):
    _, _, job = document(db)
    def fail(*_):
        raise RuntimeError("secret content must not appear in the saved error")
    monkeypatch.setattr(priority, "process_single_job", fail)
    priority.reset_shutdown_flag()
    priority.run_priority_iteration(db, cutoff=CUTOFF, history_ids=[], state={"fg_cursor": 0})
    db.refresh(job)
    assert job.status == "failed"
    assert job.finished_at is not None
    assert "secret" not in str(job.error_details) + job.last_error
