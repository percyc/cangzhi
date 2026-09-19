"""Tests for the explicit, bounded maintenance rechunk batch enqueuer.

Covers the ``enqueue_batch`` selector/enqueuer in
``apps/worker/rechunk.py``: preview is write-free, ``--apply`` enqueues a
single ``chunking`` job per current version and is idempotent per version,
exclusions (archived / deleted / non-ready / dataset / busy), explicit id
filtering, all-mode batching, and the 1..20 limit validation.
"""

import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import apps.api.models  # noqa: F401 - register all mapped tables
from apps.api.core.db import Base
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.processing import ProcessingJob
from apps.api.models.workspaces import Workspace
from apps.api.parsers.base import StructuredContent
from apps.worker.rechunk import enqueue_batch, run_selected
from apps.worker.services.processor import ASSISTED_CHUNKING_CONFIG, STRUCTURAL_CHUNKING_CONFIG


def test_structural_policy_is_explicit_and_idempotent(session):
    workspace = _make_workspace(session)
    document, version = _make_document(session, workspace)
    _make_job(session, version, status="completed")
    preview = enqueue_batch(session, document_ids=[document.id], policy=STRUCTURAL_CHUNKING_CONFIG)
    assert preview["count"] == 1 and not preview["applied"]
    result = enqueue_batch(session, document_ids=[document.id], apply=True,
                           policy=STRUCTURAL_CHUNKING_CONFIG)
    session.flush()
    assert result["policy"] == STRUCTURAL_CHUNKING_CONFIG
    assert enqueue_batch(session, policy=STRUCTURAL_CHUNKING_CONFIG)["count"] == 0
    assert session.scalar(select(ProcessingJob).where(
        ProcessingJob.config_version == STRUCTURAL_CHUNKING_CONFIG)) is not None


def test_unknown_policy_is_rejected(session):
    with pytest.raises(ValueError, match="unsupported"):
        enqueue_batch(session, policy="untrusted")
    with pytest.raises(ValueError, match="unsupported"):
        run_selected(session, [1], policy="untrusted")


@pytest.fixture
def engine():
    return create_engine("sqlite:///:memory:")


@pytest.fixture
def session(engine):
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        yield session


def _make_workspace(session, *, status="active"):
    workspace = Workspace(
        slug=f"ws-{uuid.uuid4().hex[:12]}",
        name="Workspace",
        is_default=status == "active",
        status=status,
    )
    session.add(workspace)
    session.flush()
    return workspace


def _structured(document_type):
    return StructuredContent(document_type=document_type, blocks=[]).to_dict()


def _make_document(
    session,
    workspace,
    *,
    title="Doc",
    document_type="markdown",
    processing_status="ready",
    is_deleted=False,
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
    session.add(version)
    session.flush()
    document.current_version_id = version.id
    session.flush()
    return document, version


def _make_job(session, version, *, stage="chunking", status="created",
              config_version=None):
    job = ProcessingJob(
        document_id=version.document_id,
        document_version_id=version.id,
        stage=stage,
        status=status,
        config_version=config_version or ASSISTED_CHUNKING_CONFIG,
        idempotency_key=f"{version.id}:{stage}:{uuid.uuid4().hex[:8]}",
        retry_count=0,
        max_retries=3,
    )
    session.add(job)
    session.flush()
    return job


class TestDryRunNoWrites:
    def test_preview_selects_candidate_without_writing_jobs(self, session):
        workspace = _make_workspace(session)
        document, version = _make_document(session, workspace)

        result = enqueue_batch(session, limit=10, apply=False)

        assert result["applied"] is False
        assert result["count"] == 1
        assert result["selected"] == [
            {"document_id": document.id, "version_id": version.id,
             "file_type": "markdown"}
        ]
        jobs = session.execute(
            select(ProcessingJob).where(
                ProcessingJob.document_version_id == version.id
            )
        ).scalars().all()
        assert jobs == []

    def test_unsupported_type_is_not_selected(self, session):
        workspace = _make_workspace(session)
        _make_document(session, workspace, document_type="xlsx")

        result = enqueue_batch(session, limit=10, apply=False, document_ids=None)

        assert result["count"] == 0


class TestApplyIdempotent:
    def test_apply_enqueues_specific_current_version(self, session):
        workspace = _make_workspace(session)
        document, version = _make_document(session, workspace)

        result = enqueue_batch(session, apply=True, document_ids=[document.id])

        assert result["applied"] is True
        assert result["count"] == 1
        jobs = session.query(ProcessingJob).all()
        assert len(jobs) == 1
        job = jobs[0]
        assert job.document_id == document.id
        assert job.document_version_id == version.id
        assert job.stage == "chunking"
        assert job.status == "created"
        assert job.retry_count == 0
        assert job.max_retries == 3
        assert job.config_version == ASSISTED_CHUNKING_CONFIG
        assert job.idempotency_key == f"{version.id}:chunking:{ASSISTED_CHUNKING_CONFIG}"

    def test_apply_is_idempotent_per_version(self, session):
        workspace = _make_workspace(session)
        document, _version = _make_document(session, workspace)

        first = enqueue_batch(session, apply=True, document_ids=[document.id])
        second = enqueue_batch(session, apply=True, document_ids=[document.id])

        assert first["count"] == 1
        assert second["count"] == 0
        assert len(session.query(ProcessingJob).all()) == 1


class TestExclusions:
    def test_archived_workspace_is_excluded(self, session):
        workspace = _make_workspace(session, status="archived")
        _make_document(session, workspace)
        assert enqueue_batch(session, apply=False)["count"] == 0

    def test_deleted_document_is_excluded(self, session):
        workspace = _make_workspace(session)
        _make_document(session, workspace, is_deleted=True)
        assert enqueue_batch(session, apply=False)["count"] == 0

    def test_non_ready_version_is_excluded(self, session):
        workspace = _make_workspace(session)
        _make_document(session, workspace, processing_status="parsing")
        assert enqueue_batch(session, apply=False)["count"] == 0

    def test_spreadsheet_dataset_is_excluded(self, session):
        workspace = _make_workspace(session)
        _make_document(session, workspace, document_type="xlsx")
        assert enqueue_batch(session, apply=False)["count"] == 0

    def test_version_with_running_job_is_excluded(self, session):
        workspace = _make_workspace(session)
        _document, version = _make_document(session, workspace)
        _make_job(session, version, stage="chunking", status="processing")
        assert enqueue_batch(session, apply=False)["count"] == 0

    def test_version_with_created_structural_job_is_excluded(self, session):
        workspace = _make_workspace(session)
        _document, version = _make_document(session, workspace)
        _make_job(session, version, stage="parsing", status="created")
        assert enqueue_batch(session, apply=False)["count"] == 0

    def test_embedding_created_job_does_not_block(self, session):
        workspace = _make_workspace(session)
        document, version = _make_document(session, workspace)
        _make_job(session, version, stage="embedding", status="created")
        assert enqueue_batch(session, apply=False)["count"] == 1
        assert enqueue_batch(session, apply=False)["selected"][0]["version_id"] == version.id


class TestExplicitIdsFiltering:
    def test_ids_select_only_requested_documents(self, session):
        workspace = _make_workspace(session)
        first, _first_version = _make_document(session, workspace)
        second, _second_version = _make_document(session, workspace)
        _make_document(session, workspace)

        result = enqueue_batch(session, document_ids=[first.id, second.id])

        ids = {row["document_id"] for row in result["selected"]}
        assert ids == {first.id, second.id}

    def test_unknown_and_ineligible_ids_are_omitted(self, session):
        workspace = _make_workspace(session)
        document, _version = _make_document(session, workspace)

        result = enqueue_batch(
            session, document_ids=[document.id, 999_999]
        )

        assert [row["document_id"] for row in result["selected"]] == [document.id]

    def test_ids_validation(self, session):
        with pytest.raises(ValueError):
            enqueue_batch(session, document_ids=[])
        with pytest.raises(ValueError):
            enqueue_batch(session, document_ids=[0])
        with pytest.raises(ValueError):
            enqueue_batch(session, document_ids=[-3])
        with pytest.raises(ValueError):
            enqueue_batch(session, document_ids=[1.5])


class TestSelectedRunner:
    def test_never_runs_old_parsing_or_embedding_jobs(self, session, monkeypatch):
        workspace = _make_workspace(session)
        document, version = _make_document(session, workspace)
        _make_job(session, version, stage="parsing")
        _make_job(session, version, stage="embedding")
        calls = []
        monkeypatch.setattr("apps.worker.rechunk.process_single_job", lambda *a: calls.append(a))
        assert run_selected(session, [document.id])["attempted"] == 0
        assert not calls

    def test_only_selected_new_jobs_and_stop_on_failure(self, session, monkeypatch):
        workspace = _make_workspace(session)
        document, version = _make_document(session, workspace)
        other, other_version = _make_document(session, workspace)
        other_job = _make_job(session, other_version)
        selected = _make_job(session, version)
        calls = []
        def fail(db, job_id):
            calls.append(job_id)
            return False
        monkeypatch.setattr("apps.worker.rechunk.process_single_job", fail)
        assert run_selected(session, [document.id]) == {"attempted": 1, "completed": 0}
        assert calls == [selected.id]
        assert other_job.status == "created"

    def test_new_policy_embeddings_are_eligible(self, session, monkeypatch):
        workspace = _make_workspace(session)
        document, version = _make_document(session, workspace)
        version.meta = {"chunk_config_version": ASSISTED_CHUNKING_CONFIG}
        job = _make_job(session, version, stage="embedding")
        def complete(db, job_id):
            db.get(ProcessingJob, job_id).status = "completed"
            db.commit()
            return True
        monkeypatch.setattr("apps.worker.rechunk.process_single_job", complete)
        assert run_selected(session, [document.id], max_jobs=1)["completed"] == 1
        assert job.status == "completed"


class TestLimitValidation:
    def test_too_low_rejected(self, session):
        with pytest.raises(ValueError):
            enqueue_batch(session, limit=0)

    def test_too_high_rejected(self, session):
        with pytest.raises(ValueError):
            enqueue_batch(session, limit=21)

    def test_negative_rejected(self, session):
        with pytest.raises(ValueError):
            enqueue_batch(session, limit=-5)


class TestAllModeBounded:
    def test_all_defaults_to_bounded_batch(self, session):
        workspace = _make_workspace(session)
        for index in range(5):
            _make_document(session, workspace, title=f"Doc {index}")

        result = enqueue_batch(session, limit=3)

        assert result["count"] == 3
        assert len(result["selected"]) == 3

    def test_all_picks_lowest_document_ids_first(self, session):
        workspace = _make_workspace(session)
        _make_document(session, workspace, title="A")
        _make_document(session, workspace, title="B")
        _make_document(session, workspace, title="C")

        result = enqueue_batch(session, limit=2)

        ids = [row["document_id"] for row in result["selected"]]
        assert len(ids) == 2
        assert ids == sorted(ids)
