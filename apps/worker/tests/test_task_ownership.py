"""In-process (SQLite) tests for the ADR-035 task ownership / lease / fencing substrate.

These cover the purely-testable core of :mod:`apps.worker.services.lease`:
unique token sizes, the recovery predicate, lease stamping (stage-aware hard
deadline), the write fence raising on a stale/lost attempt, and the raw-DML
detector (including CTE data-modifying statements). They never touch the
supervised PostgreSQL path (which is gated behind ``task_execution_enabled``
and the isolated reliability tests) and never require a shared schema.

PostgreSQL-only semantics (``clock_timestamp()`` refresh CAS, advisory-lock
serialization, ``now()``-free fencing) are exercised only in the gated
:mod:`apps.worker.tests.test_reliability_postgres`, never here.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import apps.api.models  # noqa: F401 - register all mapped tables
from apps.api.core.db import Base
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.processing import ProcessingJob
from apps.worker.services import lease
from apps.worker.services.lease import (
    StaleAttemptError,
    _is_dml,
    db_utc_now,
    is_recoverable_claim,
    new_attempt_id,
    stamp_lease,
    utc_now,
    worker_id,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as s:
        yield s


def _version(session):
    doc = Document(title="t", source_type=DocumentSourceType.note)
    session.add(doc)
    session.flush()
    version = DocumentVersion(
        document_id=doc.id, version_number=1, content_hash="h1",
        raw_content="x", processing_status="ready",
    )
    session.add(version)
    session.flush()
    return doc, version


def _job(session, *, version, status="created", **kw):
    job = ProcessingJob(
        document_id=version.document_id,
        document_version_id=version.id,
        stage=kw.pop("stage", "parsing"),
        status=status,
        idempotency_key=kw.pop("key", "k"),
        config_version="1",
        next_retry_at=None,
        **kw,
    )
    session.add(job)
    session.flush()
    return job


class TestTokenSizes:
    def test_attempt_id_is_unique_and_fits_string64(self, session):
        ids = {new_attempt_id(job) for job in range(500) for _ in range(3)}
        assert len(ids) == 1500
        for token in ids:
            assert isinstance(token, str)
            assert len(token) <= 64

    def test_worker_id_fits_string64(self):
        wid = worker_id()
        assert isinstance(wid, str)
        assert 0 < len(wid) <= 64
        # Stable within a process.
        assert worker_id() == wid


class TestRecoveryPredicate:
    def test_orphan_without_lease_is_never_recoverable(self, session):
        _, version = _version(session)
        job = _job(session, version=version, stage="parsing", status="processing")
        session.commit()
        assert job.lease_until is None
        assert is_recoverable_claim(job, utc_now()) is False

    def test_expired_lease_is_recoverable(self, session):
        _, version = _version(session)
        now = utc_now()
        job = _job(session, version=version, stage="parsing", status="processing")
        job.attempt_id = new_attempt_id(job.id)
        job.worker_id = worker_id()
        job.lease_until = now - datetime.timedelta(seconds=5)
        session.commit()
        assert is_recoverable_claim(job, now) is True

    def test_live_lease_is_not_recoverable(self, session):
        _, version = _version(session)
        now = utc_now()
        job = _job(session, version=version, stage="parsing", status="processing")
        job.attempt_id = new_attempt_id(job.id)
        job.worker_id = worker_id()
        job.lease_until = now + datetime.timedelta(seconds=30)
        session.commit()
        assert is_recoverable_claim(job, now) is False

    def test_non_processing_is_not_recoverable(self, session):
        _, version = _version(session)
        now = utc_now()
        job = _job(session, version=version, stage="parsing", status="retry")
        job.attempt_id = new_attempt_id(job.id)
        job.lease_until = now - datetime.timedelta(seconds=5)
        session.commit()
        assert is_recoverable_claim(job, now) is False


class TestStampLease:
    def test_stamps_attempt_lease_and_stage_deadline(self, session):
        from sqlalchemy import select
        _, version = _version(session)
        job = _job(session, version=version, stage="parsing", status="created")
        session.commit()
        id_before = job.id
        stamp_lease(session, job, utc_now())
        assert job.attempt_id is not None and len(job.attempt_id) <= 64
        assert job.worker_id is not None
        assert job.lease_until is not None and job.lease_until > job.heartbeat_at
        assert job.hard_deadline is not None
        assert job.hard_deadline > job.lease_until
        assert job.attempt_count == 1
        # Embedded code (short deadline) vs parsing (large deadline).
        embed = _job(session, version=version, stage="embedding", key="e")
        stamp_lease(session, embed, utc_now())
        session.commit()
        assert embed.hard_deadline < job.hard_deadline + datetime.timedelta(hours=1)


class TestRawDMLDetector:
    def test_detects_orm_and_text_dml(self):
        from sqlalchemy import delete, select, update
        assert _is_dml(update(ProcessingJob).values(status="failed")) is True
        assert _is_dml(delete(ProcessingJob).where(ProcessingJob.id == 1)) is True
        assert _is_dml(select(ProcessingJob)) is False
        assert _is_dml(text("UPDATE processing_jobs SET status='x'")) is True
        assert _is_dml(text("INSERT INTO processing_jobs (id) VALUES (1)")) is True
        assert _is_dml(text("DELETE FROM processing_jobs")) is True

    def test_detects_cte_data_modifying_statements(self):
        assert (
            _is_dml(
                text("WITH x AS (SELECT 1 AS id) UPDATE processing_jobs SET status='x'")
            )
            is True
        )
        assert (
            _is_dml(
                text("WITH x AS (SELECT id FROM processing_jobs) DELETE FROM processing_jobs")
            )
            is True
        )
        # A pure WITH...SELECT is not DML.
        assert (
            _is_dml(text("WITH x AS (SELECT 1 AS id) SELECT * FROM x"))
            is False
        )


class TestLeaseFence:
    def test_fence_raises_when_attempt_is_recovered(self, session):
        doc, version = _version(session)
        job = _job(session, version=version, stage="parsing", status="processing")
        job.attempt_id = new_attempt_id(job.id)
        job.worker_id = worker_id()
        job.lease_until = utc_now() + datetime.timedelta(seconds=60)
        job.hard_deadline = utc_now() + datetime.timedelta(seconds=1800)
        job.config_version = "1"
        job.document_version_id = version.id
        job.document_id = doc.id
        session.commit()
        expectation = lease.capture_expectation(session, job)

        # A recovery hands the job to a new attempt.
        job2 = session.scalar(
            __import__("sqlalchemy", fromlist=["select"]).select(ProcessingJob)
            .where(ProcessingJob.id == job.id)
        )
        job2.attempt_id = new_attempt_id(job.id)
        job2.worker_id = worker_id()
        job2.lease_until = utc_now() + datetime.timedelta(seconds=120)
        job2.hard_deadline = utc_now() + datetime.timedelta(seconds=1800)
        session.commit()

        fence = lease.LeaseFence(session, expectation=expectation)
        with fence:
            with pytest.raises(StaleAttemptError):
                # A bulk write re-verifies and fails closed.
                session.execute(
                    text("UPDATE processing_jobs SET last_error='late write'")
                )

    def test_fence_holds_when_attempt_is_current(self, session):
        doc, version = _version(session)
        job = _job(session, version=version, stage="parsing", status="processing")
        job.attempt_id = new_attempt_id(job.id)
        job.worker_id = worker_id()
        job.lease_until = utc_now() + datetime.timedelta(seconds=60)
        job.hard_deadline = utc_now() + datetime.timedelta(seconds=1800)
        job.config_version = "1"
        job.document_version_id = version.id
        job.document_id = doc.id
        session.commit()
        expectation = lease.capture_expectation(session, job)
        fence = lease.LeaseFence(session, expectation=expectation)
        with fence:
            job.last_error = "in-flight update"
            session.add(job)
            session.commit()  # must not raise


class TestDbClock:
    def test_sqlite_falls_back_to_python_clock(self, session):
        now = db_utc_now(session)
        assert now.tzinfo is not None
        assert isinstance(now, datetime.datetime)


def test_supervised_claims_inactive_by_default(session):
    # The experimental supervised path is off by default (explicit in-process
    # test hook / feature gate), so claims keep exact legacy behaviour.
    assert lease.supervised_claims_active(session) is False