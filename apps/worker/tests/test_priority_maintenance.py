"""Tests for the priority-aware maintenance scheduler in
:mod:`apps.worker.priority_maintenance`.

The scheduler runs while the normal cangzhi Worker stays stopped and
serves two pools:

* **Foreground** — current, non-deleted documents in active
  workspaces, with ``DocumentVersion.created_at >= cutoff`` and stage
  in :data:`FOREGROUND_STAGES`. Old version jobs (even newly created
  retries against an old version) are excluded by joining the job's
  ``document_version_id`` to ``Document.current_version_id``.
* **Background** — only the explicit ``--history-ids`` allow-list,
  restricted to chunking jobs whose ``config_version`` is
  ``ASSISTED_CHUNKING_CONFIG`` or embedding jobs whose version
  stamped ``chunk_config_version`` is the same constant.

These tests are SQLite-only; they never touch PostgreSQL, the
advisory lock or the daemon signal path. The graceful stop test is
best-effort and runs the daemon in a thread, sharing a single
``StaticPool`` engine so the in-memory database stays alive across
sessions.
"""

from __future__ import annotations

import datetime
import threading
import time
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import apps.api.models  # noqa: F401 - register all mapped tables
from apps.api.core.db import Base
from apps.api.models.auth import AIRuntimeConfig
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.processing import ProcessingJob
from apps.api.models.workspaces import Workspace
from apps.api.parsers.base import StructuredContent
from apps.worker import priority_maintenance
from apps.worker.priority_maintenance import (
    ASSISTED_CHUNKING_CONFIG,
    CHUNKING_STAGE,
    EMBEDDING_STAGE,
    FOREGROUND_STAGES,
    FOREGROUND_WEIGHT,
    HISTORY_WEIGHT,
    PDF_DOCUMENT_TYPE,
    RESUME_STAGES,
    _active_embedding_profile_id,
    _claim_one_resume_job,
    _run_resume_step,
    acquire_daemon_lock,
    is_shutdown_requested,
    parse_iso8601_utc,
    release_daemon_lock,
    reset_shutdown_flag,
    run_priority_daemon,
    run_priority_iteration,
)
from apps.worker.services.processor import CHUNKING_IDEMPOTENCY, utc_now
from apps.api.models.auth import AIRuntimeConfig


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    return create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


@pytest.fixture
def session(engine):
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        yield session


def _workspace(session, *, status="active", slug=None):
    workspace = Workspace(
        slug=slug or f"ws-{uuid.uuid4().hex[:12]}",
        name="Workspace",
        is_default=status == "active",
        status=status,
    )
    session.add(workspace)
    session.flush()
    return workspace


def _structured(document_type):
    return StructuredContent(document_type=document_type, blocks=[]).to_dict()


def _document(
    session,
    workspace,
    *,
    title="Doc",
    document_type="markdown",
    processing_status="ready",
    is_deleted=False,
    created_at=None,
):
    document = Document(
        title=title,
        source_type=DocumentSourceType.file,
        workspace_id=workspace.id,
        is_deleted=is_deleted,
    )
    session.add(document)
    session.flush()
    version = DocumentVersion(
        document_id=document.id,
        version_number=1,
        content_hash=f"hash-{uuid.uuid4().hex[:8]}",
        processing_status=processing_status,
        structured_content=_structured(document_type),
    )
    if created_at is not None:
        version.created_at = created_at
    session.add(version)
    session.flush()
    document.current_version_id = version.id
    session.flush()
    return document, version


def _second_version(
    session,
    document,
    *,
    document_type="markdown",
    created_at=None,
):
    """Create a new version for an existing document; advances current_version_id."""
    version = DocumentVersion(
        document_id=document.id,
        version_number=2,
        content_hash=f"hash-{uuid.uuid4().hex[:8]}",
        processing_status="ready",
        structured_content=_structured(document_type),
    )
    if created_at is not None:
        version.created_at = created_at
    session.add(version)
    session.flush()
    document.current_version_id = version.id
    session.flush()
    return version


def _job(
    session,
    document,
    version,
    *,
    stage="parsing",
    status="created",
    config_version=CHUNKING_IDEMPOTENCY,
    next_retry_at=None,
    created_at=None,
    embedding_profile_id=None,
    embedding_chunk_id=None,
):
    job = ProcessingJob(
        document_id=document.id,
        document_version_id=version.id,
        stage=stage,
        status=status,
        config_version=config_version,
        idempotency_key=(
            f"{version.id}:{stage}:{uuid.uuid4().hex[:8]}"
        ),
        retry_count=0,
        max_retries=3,
        next_retry_at=next_retry_at,
        embedding_profile_id=embedding_profile_id,
        embedding_chunk_id=embedding_chunk_id,
    )
    if created_at is not None:
        job.created_at = created_at
    session.add(job)
    session.commit()
    return job


def _cutoff(delta_minutes=60):
    return utc_now() - datetime.timedelta(minutes=delta_minutes)


# ---------------------------------------------------------------------------
# Foreground selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('stage', ['parsing', 'stored', 'chunking', 'understanding', 'preview', 'dataset_catalog', 'dataset_artifact'])
def test_resume_pipeline_opt_in_and_downstream(session, stage):
    ws = _workspace(session)
    doc, version = _document(session, ws, created_at=_cutoff(200))
    job = _job(session, doc, version, stage=stage)
    assert priority_maintenance._claim_one_resume_job(session, [], stage) is None
    assert priority_maintenance._claim_one_resume_job(session, [doc.id + 100], stage) is None
    assert priority_maintenance._claim_one_resume_job(session, [doc.id], stage) == job.id
    assert job.config_version == CHUNKING_IDEMPOTENCY
    job.status = 'completed'
    session.commit()
    downstream = _job(session, doc, version, stage='understanding')
    assert priority_maintenance._claim_one_resume_job(session, [doc.id], 'understanding') == downstream.id


@pytest.mark.parametrize('boundary', ['deleted', 'archived', 'old_version', 'failed', 'processing', 'completed', 'future_retry', 'enhancement'])
def test_resume_boundaries(session, boundary):
    ws = _workspace(session, status='archived' if boundary == 'archived' else 'active')
    doc, version = _document(session, ws, is_deleted=boundary == 'deleted')
    stage = 'knowledge_enhancement' if boundary == 'enhancement' else 'parsing'
    status = boundary if boundary in {'failed', 'processing', 'completed'} else 'created'
    job = _job(session, doc, version, stage=stage, status=status,
               next_retry_at=utc_now()+datetime.timedelta(hours=1) if boundary == 'future_retry' else None)
    if boundary == 'old_version':
        _second_version(session, doc)
        session.commit()
    assert priority_maintenance._claim_one_resume_job(session, [doc.id], stage) is None
    session.refresh(job)
    assert job.status == status


def test_resume_active_profile_only_and_due_retry(session):
    ws = _workspace(session)
    doc, version = _document(session, ws)
    old = _job(session, doc, version, stage='embedding')
    old.embedding_profile_id = 4
    current = _job(session, doc, version, stage='embedding', status='retry')
    current.embedding_profile_id = 6
    session.commit()
    assert priority_maintenance._claim_one_resume_job(session, [doc.id], 'embedding') is None
    session.add(AIRuntimeConfig(singleton_key='singleton', provider='disabled', active_embedding_profile_id=6))
    session.commit()
    assert priority_maintenance._claim_one_resume_job(session, [doc.id], 'embedding') == current.id
    session.refresh(old)
    assert old.status == 'created'


def test_resume_stage_rotation(session, monkeypatch):
    ws = _workspace(session)
    doc, version = _document(session, ws)
    first = _job(session, doc, version, stage='parsing')
    _job(session, doc, version, stage='parsing')
    second = _job(session, doc, version, stage='chunking')
    calls = []
    monkeypatch.setattr(priority_maintenance, '_dispatch', lambda s, job_id, **kw: calls.append(job_id))
    state = {}
    assert priority_maintenance._run_resume_step(session, state, [doc.id])
    assert priority_maintenance._run_resume_step(session, state, [doc.id])
    assert calls == [first.id, second.id]


def test_resume_preserves_foreground_weight_and_history_fairness(session, monkeypatch):
    reset_shutdown_flag()
    calls = []
    def mark(name):
        calls.append(name)
        return True
    monkeypatch.setattr(priority_maintenance, '_run_foreground_step', lambda *args: mark('new'))
    monkeypatch.setattr(priority_maintenance, '_run_resume_step', lambda *args: mark('resume'))
    monkeypatch.setattr(priority_maintenance, '_run_history_step', lambda *args: mark('assisted'))
    state = {'fg_cursor': 0, 'resume_ids': [1]}
    for _ in range(2):
        assert run_priority_iteration(session, cutoff=_cutoff(), history_ids=[2], state=state) == 5
    assert calls == ['new'] * 4 + ['resume'] + ['new'] * 4 + ['assisted']


class TestForegroundCutoff:
    def test_selects_current_version_at_or_after_cutoff(self, session):
        workspace = _workspace(session)
        document, version = _document(session, workspace, created_at=utc_now())
        job = _job(session, document, version, stage=CHUNKING_STAGE)

        claimed = priority_maintenance._claim_one_foreground_job(
            session, cutoff=_cutoff()
        )

        assert claimed == job.id
        session.refresh(job)
        assert job.status == "processing"

    def test_excludes_current_version_before_cutoff(self, session):
        workspace = _workspace(session)
        document, version = _document(
            session,
            workspace,
            created_at=utc_now() - datetime.timedelta(days=1),
        )
        _job(session, document, version)

        claimed = priority_maintenance._claim_one_foreground_job(
            session, cutoff=_cutoff()
        )

        assert claimed is None

    def test_cutoff_uses_version_created_at_not_job_created_at(self, session):
        """A job created today for a *stale* version (created days ago) is
        excluded: the filter is on ``versions.created_at``, not
        ``jobs.created_at``. This is the "cutoff versions not jobcreated"
        property.
        """

        workspace = _workspace(session)
        document, version = _document(
            session,
            workspace,
            created_at=utc_now() - datetime.timedelta(days=2),
        )
        # Job is *newly created* today but points to the stale version.
        _job(session, document, version, created_at=utc_now())

        claimed = priority_maintenance._claim_one_foreground_job(
            session, cutoff=_cutoff()
        )

        assert claimed is None

    def test_newly_created_retry_against_old_version_is_excluded(self, session):
        """Even when the version is recent, a job for a *non-current*
        version is excluded. Document gains v1, then v2; a retry job
        is dispatched today against v1; v2 is the current version.
        Both the version and the job are recent, but the job must not
        be foreground because v1 is no longer the current version.
        """

        workspace = _workspace(session)
        document, old_version = _document(
            session,
            workspace,
            title="Document",
            created_at=utc_now() - datetime.timedelta(minutes=10),
        )
        new_version = _second_version(
            session,
            document,
            created_at=utc_now() - datetime.timedelta(minutes=5),
        )
        old_job = _job(
            session,
            document,
            old_version,
            status="retry",
            created_at=utc_now(),
        )
        new_job = _job(session, document, new_version, stage="embedding")

        claimed = priority_maintenance._claim_one_foreground_job(
            session, cutoff=_cutoff()
        )

        assert claimed == new_job.id
        assert claimed != old_job.id


class TestForegroundWorkspaceAndDocument:
    def test_archived_workspace_is_excluded(self, session):
        workspace = _workspace(session, status="archived")
        document, version = _document(session, workspace, created_at=utc_now())
        _job(session, document, version)

        assert (
            priority_maintenance._claim_one_foreground_job(
                session, cutoff=_cutoff()
            )
            is None
        )

    def test_deleted_document_is_excluded(self, session):
        workspace = _workspace(session)
        document, version = _document(
            session,
            workspace,
            created_at=utc_now(),
            is_deleted=True,
        )
        _job(session, document, version)

        assert (
            priority_maintenance._claim_one_foreground_job(
                session, cutoff=_cutoff()
            )
            is None
        )


class TestForegroundJobState:
    def test_due_retry_is_claimable(self, session):
        workspace = _workspace(session)
        document, version = _document(session, workspace, created_at=utc_now())
        due = _job(
            session,
            document,
            version,
            status="retry",
            next_retry_at=utc_now() - datetime.timedelta(minutes=1),
        )
        not_due = _job(
            session,
            document,
            version,
            stage="embedding",
            status="retry",
            next_retry_at=utc_now() + datetime.timedelta(hours=1),
        )

        claimed = priority_maintenance._claim_one_foreground_job(
            session, cutoff=_cutoff()
        )

        assert claimed == due.id
        session.refresh(not_due)
        assert not_due.status == "retry"

    def test_failed_or_processing_jobs_are_excluded(self, session):
        workspace = _workspace(session)
        document, version = _document(session, workspace, created_at=utc_now())
        failed_job = _job(
            session, document, version, stage="parsing", status="failed"
        )
        processing_job = _job(
            session, document, version, stage="embedding", status="processing"
        )
        eligible = _job(
            session, document, version, stage="understanding"
        )

        claimed = priority_maintenance._claim_one_foreground_job(
            session, cutoff=_cutoff()
        )

        assert claimed == eligible.id
        assert claimed != failed_job.id
        assert claimed != processing_job.id
        session.refresh(failed_job)
        session.refresh(processing_job)
        assert failed_job.status == "failed"
        assert processing_job.status == "processing"

    def test_legacy_stored_stage_is_claimable(self, session):
        """The legacy ``stored`` alias is reachable alongside ``parsing``."""

        workspace = _workspace(session)
        document, version = _document(session, workspace, created_at=utc_now())
        stored_job = _job(session, document, version, stage="stored")

        claimed = priority_maintenance._claim_one_foreground_job(
            session, cutoff=_cutoff()
        )

        assert claimed == stored_job.id


class TestForegroundStages:
    def test_all_documented_stages_are_claimable(self, session):
        for stage in FOREGROUND_STAGES:
            workspace = _workspace(
                session, slug=f"ws-{stage}-{uuid.uuid4().hex[:8]}"
            )
            document, version = _document(
                session, workspace, created_at=utc_now()
            )
            _job(session, document, version, stage=stage)

        seen = set()
        for _ in range(len(FOREGROUND_STAGES)):
            claimed = priority_maintenance._claim_one_foreground_job(
                session, cutoff=_cutoff()
            )
            assert claimed is not None
            stage = session.get(ProcessingJob, claimed).stage
            seen.add(stage)
        assert seen == set(FOREGROUND_STAGES)


# ---------------------------------------------------------------------------
# Background authorization
# ---------------------------------------------------------------------------


class TestBackgroundAuthorization:
    def test_without_history_ids_nothing_is_claimed(self, session):
        workspace = _workspace(session)
        document, version = _document(
            session, workspace, document_type="pdf", created_at=utc_now()
        )
        _job(
            session,
            document,
            version,
            stage=CHUNKING_STAGE,
            config_version=ASSISTED_CHUNKING_CONFIG,
        )

        assert (
            priority_maintenance._claim_one_background_job(session, [])
            is None
        )

    def test_history_ids_must_match_allow_list(self, session):
        workspace = _workspace(session)
        document, version = _document(
            session, workspace, document_type="pdf", created_at=utc_now()
        )
        authorised = _job(
            session,
            document,
            version,
            stage=CHUNKING_STAGE,
            config_version=ASSISTED_CHUNKING_CONFIG,
        )
        _job(
            session,
            document,
            version,
            stage="embedding",
            config_version=ASSISTED_CHUNKING_CONFIG,
        )

        claimed = priority_maintenance._claim_one_background_job(
            session, [authorised.id]
        )

        assert claimed == authorised.id

    def test_legacy_chunking_jobs_are_excluded_from_history(self, session):
        """History never runs jobs without the new config_version; the
        authorisation list is the only widening of the filter.
        """

        workspace = _workspace(session)
        document, version = _document(
            session, workspace, document_type="pdf", created_at=utc_now()
        )
        legacy = _job(
            session,
            document,
            version,
            stage=CHUNKING_STAGE,
            config_version=CHUNKING_IDEMPOTENCY,
        )

        assert (
            priority_maintenance._claim_one_background_job(
                session, [legacy.id]
            )
            is None
        )

    def test_non_chunking_or_embedding_history_jobs_are_excluded(self, session):
        """Only the chunking (with new config) and embedding (with new
        chunk_config_version) stages are eligible. Understanding,
        parsing, dataset_artifact, etc. never run on the history
        path even when their IDs are listed.
        """

        workspace = _workspace(session)
        document, version = _document(
            session, workspace, document_type="pdf", created_at=utc_now()
        )
        understanding = _job(
            session,
            document,
            version,
            stage="understanding",
            config_version=ASSISTED_CHUNKING_CONFIG,
        )
        parsing = _job(
            session,
            document,
            version,
            stage="parsing",
            config_version=ASSISTED_CHUNKING_CONFIG,
        )

        assert (
            priority_maintenance._claim_one_background_job(
                session, [understanding.id, parsing.id]
            )
            is None
        )

    def test_embedding_history_requires_chunk_config_version(self, session):
        workspace = _workspace(session)
        document, version = _document(
            session, workspace, document_type="pdf", created_at=utc_now()
        )
        eligible = _job(
            session,
            document,
            version,
            stage=EMBEDDING_STAGE,
            config_version="some-old-fingerprint",
        )
        version.meta = {"chunk_config_version": ASSISTED_CHUNKING_CONFIG}
        session.add(version)
        session.flush()
        not_eligible = _job(
            session,
            document,
            version,
            stage=EMBEDDING_STAGE,
            config_version="another-fingerprint",
        )
        not_eligible_doc, not_eligible_version = _document(
            session,
            _workspace(session, slug=f"ws-{uuid.uuid4().hex[:8]}"),
            created_at=utc_now(),
        )
        not_eligible_with_legacy_meta = _job(
            session,
            not_eligible_doc,
            not_eligible_version,
            stage=EMBEDDING_STAGE,
            config_version="some-old-fingerprint",
        )

        claimed = priority_maintenance._claim_one_background_job(
            session, [eligible.id, not_eligible.id, not_eligible_with_legacy_meta.id]
        )

        assert claimed == eligible.id
        session.refresh(not_eligible)
        session.refresh(not_eligible_with_legacy_meta)
        assert not_eligible.status == "created"
        assert not_eligible_with_legacy_meta.status == "created"

    def test_archived_workspace_excludes_history_job(self, session):
        workspace = _workspace(session, status="archived")
        document, version = _document(
            session, workspace, document_type="pdf", created_at=utc_now()
        )
        job = _job(
            session,
            document,
            version,
            stage=CHUNKING_STAGE,
            config_version=ASSISTED_CHUNKING_CONFIG,
        )

        assert (
            priority_maintenance._claim_one_background_job(
                session, [job.id]
            )
            is None
        )


# ---------------------------------------------------------------------------
# PDF opt-in vs datasets
# ---------------------------------------------------------------------------


class TestPdfOptIn:
    def test_new_pdf_chunking_job_is_flipped_to_assisted(self, session):
        workspace = _workspace(session)
        document, version = _document(
            session,
            workspace,
            document_type=PDF_DOCUMENT_TYPE,
            created_at=utc_now(),
        )
        job = _job(
            session,
            document,
            version,
            stage=CHUNKING_STAGE,
            config_version=CHUNKING_IDEMPOTENCY,
        )
        original_key = job.idempotency_key

        claimed = priority_maintenance._claim_one_foreground_job(
            session, cutoff=_cutoff()
        )

        assert claimed == job.id
        session.refresh(job)
        assert job.config_version == ASSISTED_CHUNKING_CONFIG
        # Idempotency key and version row must not change.
        assert job.idempotency_key == original_key
        session.refresh(version)
        assert version.id != job.document_version_id or version.id == job.document_version_id

    def test_non_pdf_chunking_job_is_not_flipped(self, session):
        workspace = _workspace(session)
        document, version = _document(
            session,
            workspace,
            document_type="markdown",
            created_at=utc_now(),
        )
        job = _job(
            session,
            document,
            version,
            stage=CHUNKING_STAGE,
            config_version=CHUNKING_IDEMPOTENCY,
        )

        priority_maintenance._claim_one_foreground_job(
            session, cutoff=_cutoff()
        )
        session.refresh(job)
        assert job.config_version == CHUNKING_IDEMPOTENCY

    def test_dataset_chunking_job_is_not_flipped(self, session):
        """Datasets use the dataset pipeline; the opt-in only targets
        ``document_type == 'pdf'`` and never changes xlsx/database rows.
        """

        workspace = _workspace(session)
        document, version = _document(
            session,
            workspace,
            document_type="xlsx",
            created_at=utc_now(),
        )
        job = _job(
            session,
            document,
            version,
            stage=CHUNKING_STAGE,
            config_version=CHUNKING_IDEMPOTENCY,
        )

        priority_maintenance._claim_one_foreground_job(
            session, cutoff=_cutoff()
        )
        session.refresh(job)
        assert job.config_version == CHUNKING_IDEMPOTENCY

    def test_already_assisted_job_is_not_rewritten(self, session):
        workspace = _workspace(session)
        document, version = _document(
            session,
            workspace,
            document_type=PDF_DOCUMENT_TYPE,
            created_at=utc_now(),
        )
        job = _job(
            session,
            document,
            version,
            stage=CHUNKING_STAGE,
            config_version=ASSISTED_CHUNKING_CONFIG,
        )

        priority_maintenance._claim_one_foreground_job(
            session, cutoff=_cutoff()
        )
        session.refresh(job)
        assert job.config_version == ASSISTED_CHUNKING_CONFIG

    def test_idempotency_key_is_preserved(self, session):
        workspace = _workspace(session)
        document, version = _document(
            session,
            workspace,
            document_type=PDF_DOCUMENT_TYPE,
            created_at=utc_now(),
        )
        job = _job(
            session,
            document,
            version,
            stage=CHUNKING_STAGE,
            config_version=CHUNKING_IDEMPOTENCY,
        )
        original_key = job.idempotency_key

        priority_maintenance._claim_one_foreground_job(
            session, cutoff=_cutoff()
        )
        session.refresh(job)
        assert job.idempotency_key == original_key


# ---------------------------------------------------------------------------
# Weighted schedule
# ---------------------------------------------------------------------------


class TestWeightedSchedule:
    """The cycle drains 4 foreground claims then 1 history claim when
    both pools are non-empty; if one pool is empty the other takes
    over so we never idle while work is waiting.
    """

    def test_4_foreground_then_1_history_when_both_present(
        self, session, monkeypatch
    ):
        foreground_ids = []
        history_ids = []
        # Use 4 distinct foreground stages so the cycle can claim
        # ``FOREGROUND_WEIGHT`` jobs in one iteration. The stages
        # must appear consecutively in ``FOREGROUND_STAGES`` so the
        # cycle visits them without burning attempts on empty slots.
        foreground_stages = (
            "parsing",
            "chunking",
            "dataset_catalog",
            "dataset_artifact",
        )
        for stage in foreground_stages:
            workspace = _workspace(
                session, slug=f"ws-{stage}-{uuid.uuid4().hex[:8]}"
            )
            document, version = _document(
                session, workspace, created_at=utc_now()
            )
            job = _job(session, document, version, stage=stage)
            foreground_ids.append(job.id)
        for index in range(3):
            workspace = _workspace(
                session, slug=f"hws-{index}-{uuid.uuid4().hex[:8]}"
            )
            document, version = _document(
                session,
                workspace,
                document_type=PDF_DOCUMENT_TYPE,
                created_at=utc_now(),
            )
            job = _job(
                session,
                document,
                version,
                stage=CHUNKING_STAGE,
                config_version=ASSISTED_CHUNKING_CONFIG,
            )
            history_ids.append(job.id)

        # Stub process_single_job so the iteration just records the
        # job ID and returns success without touching real models.
        order = []
        monkeypatch.setattr(
            priority_maintenance,
            "process_single_job",
            lambda _session, job_id: order.append(job_id) or True,
        )
        state = {"fg_cursor": 0, "fg_weight": 0}
        processed = run_priority_iteration(
            session,
            cutoff=_cutoff(),
            history_ids=history_ids,
            state=state,
        )

        assert processed == FOREGROUND_WEIGHT + HISTORY_WEIGHT
        foreground_in_order = [
            job_id for job_id in order if job_id in foreground_ids
        ]
        history_in_order = [
            job_id for job_id in order if job_id in history_ids
        ]
        assert len(foreground_in_order) == FOREGROUND_WEIGHT
        assert len(history_in_order) == HISTORY_WEIGHT
        # All foreground claims precede the history claim.
        first_history = order.index(history_in_order[0])
        assert all(
            order.index(job_id) < first_history
            for job_id in foreground_in_order
        )

    def test_falls_back_to_history_when_foreground_empty(
        self, session, monkeypatch
    ):
        history_id = _job(
            session,
            *_document(
                session,
                _workspace(session, slug=f"ws-{uuid.uuid4().hex[:8]}"),
                document_type=PDF_DOCUMENT_TYPE,
                created_at=utc_now(),
            ),
            stage=CHUNKING_STAGE,
            config_version=ASSISTED_CHUNKING_CONFIG,
        ).id

        order = []
        monkeypatch.setattr(
            priority_maintenance,
            "process_single_job",
            lambda _session, job_id: order.append(job_id) or True,
        )
        state = {"fg_cursor": 0, "fg_weight": 0}
        processed = run_priority_iteration(
            session,
            cutoff=_cutoff(),
            history_ids=[history_id],
            state=state,
        )

        assert processed == 1
        assert order == [history_id]

    def test_falls_back_to_foreground_when_history_unauthorised(
        self, session, monkeypatch
    ):
        workspace = _workspace(session)
        document, version = _document(
            session, workspace, created_at=utc_now()
        )
        foreground_id = _job(
            session, document, version, stage="parsing"
        ).id

        order = []
        monkeypatch.setattr(
            priority_maintenance,
            "process_single_job",
            lambda _session, job_id: order.append(job_id) or True,
        )
        state = {"fg_cursor": 0, "fg_weight": 0}
        processed = run_priority_iteration(
            session,
            cutoff=_cutoff(),
            history_ids=[],
            state=state,
        )

        assert processed == 1  # Only one job exists; never execute it repeatedly.
        assert foreground_id in order

    def test_idle_when_both_pools_empty(self, session, monkeypatch):
        monkeypatch.setattr(
            priority_maintenance,
            "process_single_job",
            lambda *_a, **_k: True,
        )
        state = {"fg_cursor": 0, "fg_weight": 0}
        processed = run_priority_iteration(
            session,
            cutoff=_cutoff(),
            history_ids=[],
            state=state,
        )
        assert processed == 0


class TestStageRoundRobin:
    def test_foreground_cycle_starts_at_parsing_and_rotates(
        self, session, monkeypatch
    ):
        stages = ["parsing", "chunking", "understanding", "preview",
                  "dataset_catalog", "dataset_artifact", "embedding",
                  "stored"]
        jobs = {}
        for stage in stages:
            workspace = _workspace(
                session, slug=f"ws-{stage}-{uuid.uuid4().hex[:8]}"
            )
            document, version = _document(
                session, workspace, created_at=utc_now()
            )
            jobs[stage] = _job(
                session, document, version, stage=stage
            ).id

        order = []
        monkeypatch.setattr(
            priority_maintenance,
            "process_single_job",
            lambda _session, job_id: order.append(job_id) or True,
        )
        state = {"fg_cursor": 0, "fg_weight": 0}
        run_priority_iteration(
            session, cutoff=_cutoff(), history_ids=[], state=state
        )

        observed_stages = [
            session.get(ProcessingJob, job_id).stage for job_id in order
        ]
        # The cycle visits the first FOREGROUND_WEIGHT distinct stages
        # in cycle order; ``parsing`` must be the first stop.
        assert observed_stages[0] == "parsing"
        assert observed_stages == list(FOREGROUND_STAGES)[: len(observed_stages)]


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


class TestArgParsing:
    def test_iso8601_accepts_z_suffix(self):
        parsed = parse_iso8601_utc("2026-09-18T12:00:00Z")
        assert parsed == datetime.datetime(
            2026, 9, 18, 12, 0, 0, tzinfo=datetime.timezone.utc
        )

    def test_iso8601_accepts_offset(self):
        parsed = parse_iso8601_utc("2026-09-18T20:00:00+08:00")
        assert parsed == datetime.datetime(
            2026, 9, 18, 12, 0, 0, tzinfo=datetime.timezone.utc
        )

    def test_iso8601_rejects_naive(self):
        with pytest.raises(Exception):
            parse_iso8601_utc("2026-09-18T12:00:00")

    def test_iso8601_rejects_empty(self):
        with pytest.raises(Exception):
            parse_iso8601_utc("")

    def test_iso8601_rejects_garbage(self):
        with pytest.raises(Exception):
            parse_iso8601_utc("not-a-date")


# ---------------------------------------------------------------------------
# Graceful stop (best-effort)
# ---------------------------------------------------------------------------


class TestGracefulStop:
    def test_shutdown_flag_is_honoured_between_iterations(
        self, session, monkeypatch
    ):
        workspace = _workspace(session)
        document, version = _document(
            session, workspace, created_at=utc_now()
        )
        _job(session, document, version, stage="parsing")

        # Pre-arm the shutdown flag; the daemon must exit before
        # claiming another job.
        reset_shutdown_flag()
        priority_maintenance._request_shutdown()
        assert is_shutdown_requested()

        order = []
        monkeypatch.setattr(
            priority_maintenance,
            "process_single_job",
            lambda *_a, **_k: True,
        )

        processed = run_priority_daemon(
            session.bind,
            cutoff=_cutoff(),
            history_ids=[],
            once=True,
            poll_interval=0,
            install_signal_handlers=False,
        )

        assert processed == 0
        assert order == []
        reset_shutdown_flag()

    def test_in_flight_job_finishes_before_loop_exits(
        self, engine, session, monkeypatch
    ):
        """A real serve loop running in a thread must finish the
        in-flight job after a shutdown request, then exit. Uses
        :class:`StaticPool` so the in-memory database is visible to
        the thread.
        """

        workspace = _workspace(session)
        document, version = _document(
            session, workspace, created_at=utc_now()
        )
        _job(session, document, version, stage="parsing")
        session.commit()

        started = threading.Event()
        finished = threading.Event()

        def slow_stub(_session, job_id):
            started.set()
            row = _session.get(ProcessingJob, job_id)
            row.status = "completed"
            row.finished_at = datetime.datetime.now(
                datetime.timezone.utc
            )
            _session.add(row)
            _session.commit()
            finished.set()
            return True

        monkeypatch.setattr(
            priority_maintenance,
            "process_single_job",
            slow_stub,
        )
        reset_shutdown_flag()

        def serve():
            run_priority_daemon(
                engine,
                cutoff=_cutoff(),
                history_ids=[],
                once=False,
                poll_interval=0.05,
                install_signal_handlers=False,
            )

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            assert started.wait(timeout=2.0), "in-flight job never started"
            priority_maintenance._request_shutdown()
            assert finished.wait(timeout=2.0), (
                "in-flight job did not finish before shutdown"
            )
            thread.join(timeout=2.0)
            assert not thread.is_alive(), "daemon did not stop after shutdown"
        finally:
            priority_maintenance._request_shutdown()
            thread.join(timeout=2.0)
            reset_shutdown_flag()

        session.expire_all()
        completed_jobs = list(
            session.scalars(
                select(ProcessingJob).where(
                    ProcessingJob.stage == "parsing"
                )
            )
        )
        assert any(row.status == "completed" for row in completed_jobs)


# ---------------------------------------------------------------------------
# Advisory lock
# ---------------------------------------------------------------------------


class TestAdvisoryLock:
    def test_sqlite_engine_returns_none(self, engine):
        connection = acquire_daemon_lock(engine)
        assert connection is None
        release_daemon_lock(connection)
