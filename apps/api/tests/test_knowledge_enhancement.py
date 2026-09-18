import copy
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session

import apps.api.models
from apps.api.core.db import Base
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.enhancement import EnhancementRun, EnhancementWindow
from apps.api.models.processing import ProcessingJob
from apps.api.models.chunks import DocumentChunk
from apps.api.models.workspaces import Workspace
from apps.api.services import knowledge_enhancement as service
from apps.api.services.workspaces import bind_workspace_context
from apps.worker.services.processor import process_single_job
from apps.worker.services import enhancement_processor as worker


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([Workspace(id=1, slug="one", name="One", settings={"other": True}),
                         Workspace(id=2, slug="two", name="Two")])
        session.commit()
        bind_workspace_context(session, 1)
        yield session
    engine.dispose()


def enable(db, budget=2, modules=None):
    result = service.save_settings(db, {"enabled": True, "modules": modules or ["chapter"],
        "call_budget": budget, "cost_acknowledged": True})
    db.commit()
    return result


def document(db, kind="docx", count=3):
    doc = Document(title="sample", source_type=DocumentSourceType.file, workspace_id=1)
    db.add(doc)
    db.flush()
    blocks = []
    for i in range(count):
        blocks.extend([{"type": "heading", "text": f"章节{i}", "heading_path": [], "level": 1},
                       {"type": "paragraph", "text": f"事实{i}以及条件说明。", "heading_path": [f"章节{i}"]}])
    version = DocumentVersion(document_id=doc.id, version_number=1, content_hash="sourcehash",
        raw_content="原文不改动", structured_content={"document_type": kind, "blocks": blocks},
        processing_status="ready", created_at=service.now())
    db.add(version)
    db.flush()
    doc.current_version_id = version.id
    db.commit()
    return doc, version


def provider(monkeypatch, callback=None, fail=False):
    calls = []
    def generate(**kwargs):
        calls.append(kwargs)
        if callback:
            callback()
        if fail:
            raise RuntimeError("PRIVATE_UPSTREAM_CREDENTIAL")
        first = json.loads(kwargs["prompt"])["segments"][0]["id"]
        return {"summary": {"text": "仅据本窗口说明。", "evidence_ids": [first]},
                "entities": [], "relations": [], "events": []}
    monkeypatch.setattr(worker, "build_provider_from_session", lambda _: SimpleNamespace(
        name="fake", _model="test", _timeout=30, generate_json=generate))
    return calls


def step(db):
    job = db.scalar(select(ProcessingJob).where(ProcessingJob.stage == service.STAGE,
        ProcessingJob.status == "created").order_by(ProcessingJob.id).limit(1))
    assert job is not None
    return process_single_job(db, job.id)


def test_default_off_and_preserves_basic_settings(db):
    doc, _ = document(db)
    assert service.start_run(db, doc.id, automatic=True) is None
    assert not service.get_settings(db)["enabled"]
    enable(db)
    service.save_settings(db, {"enabled": False, "modules": ["chapter"], "call_budget": 2})
    assert service.start_run(db, doc.id, automatic=True) is None
    assert db.get(Workspace, 1).settings["other"] is True
    assert db.scalar(select(func.count()).select_from(ProcessingJob)) == 0


def test_forward_only_enable_and_manual_history(db):
    old, version = document(db)
    version.created_at = service.now() - timedelta(days=1)
    db.commit()
    enable(db)
    assert service.start_run(db, old.id, automatic=True) is None
    assert service.start_run(db, old.id, cost_acknowledged=True)["status"] == "queued"


@pytest.mark.parametrize('enhanced', [False, True])
def test_parse_auto_enhancement_keeps_basic_understanding(db, enhanced):
    if enhanced:
        enable(db)
    doc, version = document(db, 'note', count=1)
    doc.source_type = DocumentSourceType.note
    version.raw_content, version.structured_content = '# Heading\nSome facts.', None
    job = ProcessingJob(document_id=doc.id, document_version_id=version.id, stage='parsing',
                        config_version='1', idempotency_key='test:parse:auto')
    db.add(job)
    db.commit()
    assert process_single_job(db, job.id)
    stages = set(db.scalars(select(ProcessingJob.stage)).all())
    assert {'chunking', 'understanding'}.issubset(stages)
    assert ('knowledge_enhancement' in stages) == enhanced
    assert db.get(DocumentVersion, version.id).processing_status == 'ready'


def test_unexpected_enhancement_error_never_fails_baseline(db, monkeypatch):
    from apps.worker.services import processor
    enable(db)
    doc, version = document(db)
    run = service.start_run(db, doc.id, cost_acknowledged=True)
    db.commit()
    job = db.scalar(select(ProcessingJob))
    def crash(*args):
        raise RuntimeError('PRIVATE_FAILURE')
    monkeypatch.setattr(worker, 'process_enhancement_job', crash)
    assert processor._run_claimed_job(db, job.id) == 0
    assert db.get(DocumentVersion, version.id).processing_status == 'ready'
    assert service.read_run(db, run['id'])['run']['status'] == 'failed'
    service.resume_run(db, run['id'], additional_calls=1, cost_acknowledged=True)
    assert 'PRIVATE_FAILURE' not in json.dumps(service.read_run(db, run['id']))


def test_new_explicit_enhancement_claim_does_not_revive_old_parse(db):
    from apps.worker.priority_maintenance import _claim_one_foreground_job
    enable(db)
    doc, version = document(db)
    cutoff = service.now() - timedelta(hours=1)
    version.created_at = cutoff - timedelta(days=2)
    old = ProcessingJob(document_id=doc.id, document_version_id=version.id, stage='parsing',
                        config_version='1', idempotency_key='old:parse', created_at=cutoff - timedelta(days=1))
    db.add(old)
    service.start_run(db, doc.id, cost_acknowledged=True)
    db.commit()
    chosen = _claim_one_foreground_job(db, cutoff)
    assert chosen is not None
    assert db.get(ProcessingJob, chosen).stage == 'knowledge_enhancement'
    assert db.get(ProcessingJob, old.id).status == 'created'


@pytest.mark.parametrize("kind", ["pdf", "doc", "docx", "markdown", "note", "html", "txt"])
def test_budget_progress_resume_and_source_immutable(db, monkeypatch, kind):
    enable(db)
    doc, version = document(db, kind)
    before = copy.deepcopy(version.structured_content)
    run = service.start_run(db, doc.id, cost_acknowledged=True)
    assert service.start_run(db, doc.id, cost_acknowledged=True)["id"] == run["id"]
    db.commit()
    calls = provider(monkeypatch)
    step(db)
    step(db)
    detail = service.read_run(db, run["id"], limit=1)
    assert detail["next_offset"] == 1
    assert detail["run"]["status"] == "partial"
    assert detail["run"]["completed_windows"] == detail["run"]["calls_used"] == 2
    assert detail["windows"][0]["source_segments"][0]["text"] == "章节0"
    assert len(calls) == 2
    assert service.list_runs(db, doc.id)["runs"][0]["calls_used"] == 2
    assert len(calls) == 2  # Reading cannot invoke the provider.
    service.resume_run(db, run["id"], additional_calls=1, cost_acknowledged=True)
    db.commit()
    step(db)
    detail = service.read_run(db, run["id"])
    assert detail["run"]["status"] == "completed"
    assert detail["run"]["completed_source_chars"] == detail["run"]["total_source_chars"]
    db.refresh(version)
    assert version.structured_content == before and version.raw_content == "原文不改动"
    assert db.scalar(select(func.count()).select_from(DocumentChunk)) == 0


def test_failed_model_attempts_count_and_errors_are_sanitized(db, monkeypatch):
    enable(db, budget=1)
    doc, _ = document(db)
    run = service.start_run(db, doc.id, cost_acknowledged=True)
    db.commit()
    provider(monkeypatch, fail=True)
    assert not step(db)
    detail = service.read_run(db, run["id"])
    assert detail["run"]["calls_used"] == 1
    assert detail["run"]["failed_windows"] == 1
    assert "PRIVATE_UPSTREAM_CREDENTIAL" not in json.dumps(detail)
    service.resume_run(db, run["id"], additional_calls=1, cost_acknowledged=True)
    db.commit()
    provider(monkeypatch)
    assert step(db)
    assert service.read_run(db, run["id"])["run"]["completed_windows"] == 1


def test_no_model_does_not_spend_call_and_can_resume(db, monkeypatch):
    enable(db)
    doc, _ = document(db, count=1)
    run = service.start_run(db, doc.id, cost_acknowledged=True)
    db.commit()
    monkeypatch.setattr(worker, "build_provider_from_session", lambda _: None)
    assert not step(db)
    assert service.read_run(db, run["id"])["run"]["calls_used"] == 0
    service.resume_run(db, run["id"], additional_calls=1, cost_acknowledged=True)
    db.commit()
    provider(monkeypatch)
    step(db)
    assert service.read_run(db, run["id"])["run"]["status"] == "completed"


def test_cancellation_discards_in_flight_response(db, monkeypatch):
    enable(db)
    doc, _ = document(db)
    run = service.start_run(db, doc.id, cost_acknowledged=True)
    db.commit()
    def cancel():
        service.cancel_run(db, run["id"])
        db.commit()
    provider(monkeypatch, callback=cancel)
    step(db)
    detail = service.read_run(db, run["id"])
    assert detail["run"]["status"] == "cancelled"
    assert detail["run"]["calls_used"] == 1
    assert all(w["result"] is None for w in detail["windows"])


def test_source_change_discards_result(db, monkeypatch):
    enable(db)
    doc, version = document(db)
    run = service.start_run(db, doc.id, cost_acknowledged=True)
    db.commit()
    def change():
        source = copy.deepcopy(version.structured_content)
        source["blocks"][0]["text"] = "新原文"
        version.structured_content = source
        db.commit()
    provider(monkeypatch, callback=change)
    step(db)
    detail = service.read_run(db, run["id"])
    assert detail["run"]["status"] == "stale"
    assert all(w["result"] is None for w in detail["windows"])


def test_scope_and_recycle_hide_all_run_reads(db):
    enable(db)
    doc, _ = document(db)
    run = service.start_run(db, doc.id, cost_acknowledged=True)
    db.commit()
    bind_workspace_context(db, 2)
    for action in [lambda: service.read_run(db, run["id"]), lambda: service.list_runs(db, doc.id),
                   lambda: service.cancel_run(db, run["id"])]:
        with pytest.raises(service.EnhancementError) as exc:
            action()
        assert exc.value.status == 404
    bind_workspace_context(db, 1)
    doc.is_deleted = True
    db.commit()
    with pytest.raises(service.EnhancementError):
        service.read_run(db, run["id"])


def test_expired_lease_recovery_spends_new_budget(db):
    enable(db)
    doc, _ = document(db)
    record = service.start_run(db, doc.id, cost_acknowledged=True)
    run = db.get(EnhancementRun, record["id"])
    run.status, run.calls_used, run.active_attempt = "running", 1, "old-token"
    run.lease_until = service.now() + timedelta(seconds=180)
    db.commit()
    with pytest.raises(service.EnhancementError):
        service.resume_run(db, run.id, additional_calls=1, cost_acknowledged=True)
    run.lease_until = service.now() - timedelta(seconds=1)
    db.commit()
    result = service.resume_run(db, run.id, additional_calls=1, cost_acknowledged=True)
    assert result["calls_used"] == 1 and result["call_budget"] == 3
    assert run.active_attempt is None


@pytest.mark.parametrize("kind", ["xls", "xlsx", "database_table"])
def test_datasets_bypass_enhancement(db, kind):
    enable(db)
    doc, _ = document(db, kind)
    with pytest.raises(service.EnhancementError):
        service.start_run(db, doc.id, cost_acknowledged=True)
    assert db.scalar(select(func.count()).select_from(EnhancementRun)) == 0
