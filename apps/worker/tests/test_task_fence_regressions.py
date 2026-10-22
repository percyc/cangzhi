"""Regression tests for ORM corruption and post-terminal follow-up commits."""
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

import apps.api.models  # noqa: F401
from apps.api.core.db import Base
from apps.api.models.documents import Document, DocumentVersion
from apps.api.models.processing import ProcessingJob
from apps.worker.services import lease


@pytest.fixture
def held():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        doc = Document(title="synthetic", source_type="note")
        db.add(doc)
        db.flush()
        ver = DocumentVersion(document_id=doc.id, version_number=1,
                              content_hash="original", raw_content="source")
        db.add(ver)
        db.flush()
        doc.current_version_id = ver.id
        job = ProcessingJob(document_id=doc.id, document_version_id=ver.id,
                            stage="stored", status="processing", config_version="1",
                            idempotency_key="regression")
        db.add(job)
        db.flush()
        lease.stamp_lease(db, job, lease.utc_now())
        db.commit()
        yield db, doc, ver, job, lease.capture_expectation(db, job)
    engine.dispose()


def test_terminal_flush_preserves_pending_output_and_followups(held):
    db, doc, ver, job, exp = held
    with lease.LeaseFence(db, exp):
        ver.raw_content = "parsed result"
        job.status = "completed"
        db.commit()
        # Existing parsing/chunking code has a second transaction for followups.
        db.add(ProcessingJob(document_id=doc.id, document_version_id=ver.id,
            stage="chunking", status="created", config_version="1", idempotency_key="next"))
        db.commit()
    db.expire_all()
    assert job.status == "completed"
    assert ver.raw_content == "parsed result"
    assert len(db.scalars(select(ProcessingJob)).all()) == 2


def test_pending_mutations_not_flushed_when_source_changes(held):
    db, doc, ver, job, exp = held
    db.execute(text("UPDATE document_versions SET content_hash='external' WHERE id=:id"), {"id": ver.id})
    db.commit()
    with lease.LeaseFence(db, exp), pytest.raises(lease.StaleAttemptError):
        ver.raw_content = "stale output"
        job.status = "completed"
        db.commit()
    db.rollback()
    assert ver.raw_content == "source"
    assert job.status == "processing"


def test_self_authored_hash_can_advance_but_external_mutation_cannot(held):
    db, doc, ver, job, exp = held
    with lease.LeaseFence(db, exp):
        ver.content_hash = "fetched-url-hash"
        db.commit()
        ver.raw_content = "fetched source"
        job.status = "completed"
        db.commit()
    assert ver.raw_content == "fetched source"


def test_expired_lease_rolls_back_pending_terminal_status(held):
    db, doc, ver, job, exp = held
    job.lease_until = lease.utc_now() - timedelta(seconds=1)
    db.commit()
    with lease.LeaseFence(db, exp), pytest.raises(lease.StaleAttemptError):
        job.status = "completed"
        db.commit()
    db.rollback()
    assert job.status == "processing"


def test_expired_other_job_still_blocks_document(held):
    db, doc, ver, job, exp = held
    job.lease_until = lease.utc_now() - timedelta(seconds=1)
    db.commit()
    assert lease.document_has_inflight(db, doc.id)
    assert not lease.document_has_inflight(db, doc.id, exclude_job_id=job.id)


def test_publication_lock_can_outlive_soft_lease_but_not_hard_deadline(held):
    db, doc, ver, job, exp = held
    with lease.LeaseFence(db, exp):
        # First DML validates the soft lease and takes the publication locks.
        ver.raw_content = "published"
        db.flush()
        job.lease_until = lease.utc_now() - timedelta(seconds=1)
        job.status = "completed"
        db.commit()
    assert job.status == "completed"


def test_publication_cannot_outlive_hard_deadline(held):
    db, doc, ver, job, exp = held
    with lease.LeaseFence(db, exp), pytest.raises(lease.StaleAttemptError):
        ver.raw_content = "late result"
        db.flush()
        job.hard_deadline = lease.utc_now() - timedelta(seconds=1)
        job.status = "completed"
        db.commit()
    db.rollback()
    assert ver.raw_content == "source"
    assert job.status == "processing"
