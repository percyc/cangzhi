from datetime import datetime, timedelta, timezone
import time
from unittest.mock import patch

import pytest
import sqlalchemy
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from apps.api.models.monitoring import MonitoringHistory, MonitoringMetrics, MonitoringSnapshot
from apps.api.services.monitoring import aware
from apps.worker.services.monitoring import (
    _collect_kind,
    ERROR_TIMEOUT,
    ERROR_UNAVAILABLE,
    collect_once,
    collect_capacity,
    collect_knowledge,
    collect_queue,
)


@pytest.fixture
def engine():
    engine = create_engine("sqlite://")
    MonitoringSnapshot.__table__.create(engine)
    MonitoringHistory.__table__.create(engine)
    MonitoringMetrics.__table__.create(engine)
    yield engine
    engine.dispose()


def _add_snapshot(db, kind, **kwargs):
    snapshot = MonitoringSnapshot(kind=kind, attempted_at=datetime.now(timezone.utc))
    for key, value in kwargs.items():
        setattr(snapshot, key, value)
    db.add(snapshot)
    return snapshot


def _snapshot(db, kind):
    return db.get(MonitoringSnapshot, kind)


def test_aggregations_without_content_or_filesystem_walk(engine, tmp_path):
    with engine.begin() as db:
        for sql in [
            'CREATE TABLE documents(id int, current_version_id int, is_deleted boolean, source_type text)',
            'CREATE TABLE document_versions(id int, blob_id int, preview_blob_id int)',
            'CREATE TABLE document_chunks(id int, document_version_id int, is_current boolean, role text, content_hash text)',
            'CREATE TABLE embedding_profiles(id int, status text)',
            'CREATE TABLE chunk_embeddings(chunk_id int, profile_id int, content_hash text)',
            'CREATE TABLE knowledge_datasets(document_version_id int, status text)',
            'CREATE TABLE blobs(id int, file_size int)',
            'CREATE TABLE dataset_artifacts(status text, byte_size int)',
            'CREATE TABLE processing_jobs(document_version_id int, status text, created_at timestamp)',
            "INSERT INTO documents VALUES (1,1,false,'file'),(2,2,true,'file')",
            'INSERT INTO document_versions VALUES (1,1,2),(2,1,1)',
            'INSERT INTO blobs VALUES (1,100),(2,50),(3,20)',
            "INSERT INTO dataset_artifacts VALUES ('ready',10),('building',99)",
            "INSERT INTO document_chunks VALUES (1,1,true,'child','h'),(2,2,true,'child','h'),(3,1,false,'child','h')",
            "INSERT INTO embedding_profiles VALUES (1,'active'),(2,'retired')",
            "INSERT INTO chunk_embeddings VALUES (1,1,'h'),(1,2,'h'),(2,1,'h'),(3,1,'h')",
            "INSERT INTO knowledge_datasets VALUES (1,'ready'),(2,'ready')",
            "INSERT INTO processing_jobs VALUES (1,'created','2026-10-22 00:00:00'),(2,'failed','2026-10-22 00:00:00')",
        ]:
            db.execute(text(sql))
    with Session(engine) as db:
        k = collect_knowledge(db)
        assert k['documents'] == k['chunks'] == k['active_vectors'] == k['datasets'] == 1
        assert k['trash'] == 1 and k['vectors'] == 4
        # A valid identifier is insufficient: changed, NULL and historical
        # hashes must never count as current usable embeddings.
        db.execute(text("UPDATE chunk_embeddings SET content_hash='stale' WHERE chunk_id=1 AND profile_id=1"))
        assert collect_knowledge(db)['active_vectors'] == 0
        db.execute(text("UPDATE chunk_embeddings SET content_hash=NULL WHERE chunk_id=1 AND profile_id=1"))
        assert collect_knowledge(db)['active_vectors'] == 0
        db.execute(text("UPDATE chunk_embeddings SET content_hash='h' WHERE chunk_id=1 AND profile_id=1"))
        q = collect_queue(db)
        assert q['waiting'] == 1 and q['failed'] == 0
        c = collect_capacity(db, tmp_path)
        assert c['blob_bytes'] == 170 and c['original_bytes'] == 100
        assert c['preview_bytes'] == 50 and c['other_blob_bytes'] == 20
        assert c['parquet_bytes'] == 10 and c['database_bytes'] is None
        assert c['disk']['total'] > 0
        with patch('shutil.disk_usage', side_effect=OSError):
            assert collect_capacity(db, tmp_path)['disk'] is None


def test_metric_isolation_keeps_other_success_on_single_failure(engine, tmp_path):
    now = datetime.now(timezone.utc)

    def ok_probe(db):
        return {"documents": 5, "files": 5}

    def bad_probe(db):
        raise sqlalchemy.exc.OperationalError("SELECT 1", {}, Exception("secret-password"))

    def provider(kind, path):
        if kind != "knowledge":
            return []
        return [(("documents", "files"), ok_probe), (("active_vectors",), bad_probe)]

    with patch('apps.worker.services.monitoring._probes_for', side_effect=provider):
        collect_once(engine, tmp_path, now=now)
    with Session(engine) as db:
        docs = db.get(MonitoringMetrics, ("knowledge", "documents"))
        files = db.get(MonitoringMetrics, ("knowledge", "files"))
        active = db.get(MonitoringMetrics, ("knowledge", "active_vectors"))
        assert docs.value == 5 and files.value == 5 and docs.error is None
        assert active.error == ERROR_TIMEOUT and active.value is None and active.last_success_at is None
        assert active.consecutive_failures == 1 and active.next_attempt_at is not None
        assert "secret-password" not in (active.error or "")
        # The group snapshot no longer hides the partial success.
        data = _snapshot(db, "knowledge").data
        assert data["documents"] == 5 and data["active_vectors"] is None
        queue = _snapshot(db, "queue")
        assert queue is None  # No probe ran: do not invent a first attempt.


def test_failure_retains_old_value_and_old_success_time(engine, tmp_path):
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=2)
    with Session(engine) as db, db.begin():
        db.add(MonitoringMetrics(kind="knowledge", metric="active_vectors", value=300,
                                 last_success_at=old, last_attempt_at=old))

    def bad(db):
        raise sqlalchemy.exc.OperationalError("Q", {}, Exception("boom"))

    def provider(kind, path):
        return [(("active_vectors",), bad)] if kind == "knowledge" else []

    with patch('apps.worker.services.monitoring._probes_for', side_effect=provider):
        collect_once(engine, tmp_path, now=now)
    with Session(engine) as db:
        row = db.get(MonitoringMetrics, ("knowledge", "active_vectors"))
        assert row.value == 300 and row.error == ERROR_TIMEOUT
        assert row.last_success_at.replace(tzinfo=timezone.utc) == old
        assert row.last_attempt_at.replace(tzinfo=timezone.utc) == now
        assert row.consecutive_failures == 1 and row.next_attempt_at is not None
        assert aware(row.next_attempt_at) > now  # bounded first backoff is soon but not instant


def test_budget_marks_untried_metrics_as_not_attempted_not_failure(engine, tmp_path):
    now = datetime.now(timezone.utc)
    calls = {"n": 0}

    def fake_monotonic():
        calls["n"] += 1
        return 0 if calls["n"] <= 2 else 40  # budget over: second probe never attempted

    def provider(kind, path):
        if kind != "knowledge":
            return []
        return [(("documents",), lambda db: {"documents": 5}),
                (("active_vectors",), lambda db: {"active_vectors": 9})]

    with Session(engine) as db, db.begin():
        _add_snapshot(db, "knowledge", sampled_at=None)
        db.flush()
        with patch('apps.worker.services.monitoring._probes_for', side_effect=provider), \
             patch('apps.worker.services.monitoring.time.monotonic', side_effect=fake_monotonic):
            _collect_kind(db, "knowledge", 900, now, deadline=30, storage_path=str(tmp_path), snapshot=_snapshot(db, "knowledge"))
    with Session(engine) as db:
        assert db.get(MonitoringMetrics, ("knowledge", "documents")).value == 5
        assert db.get(MonitoringMetrics, ("knowledge", "active_vectors")) is None  # never attempted, not a failure
        snapshot = _snapshot(db, "knowledge")
        assert snapshot.error is None  # no failed metric, just not-yet-attempted


def test_healthy_cadence_but_failed_metric_retries_early(engine, tmp_path):
    now = datetime.now(timezone.utc)
    healthy_last = now - timedelta(seconds=100)  # < 900s interval: not due yet
    with Session(engine) as db, db.begin():
        db.add(MonitoringMetrics(kind="queue", metric="waiting", value=2,
                                 last_success_at=healthy_last, last_attempt_at=healthy_last))
        db.add(MonitoringMetrics(kind="queue", metric="failed", value=1,
                                 last_success_at=now - timedelta(hours=1),
                                 last_attempt_at=now - timedelta(hours=1),
                                 error=ERROR_UNAVAILABLE, consecutive_failures=2,
                                 next_attempt_at=now - timedelta(seconds=5)))

    def ok_waiting(db):
        return {"waiting": 7}

    def ok_failed(db):
        return {"failed": 0}

    def provider(kind, path):
        if kind != "queue":
            return []
        return [(("waiting",), ok_waiting), (("failed",), ok_failed)]

    with Session(engine) as db, db.begin():
        snapshot = db.get(MonitoringSnapshot, "queue")
        if snapshot is None:
            snapshot = MonitoringSnapshot(kind="queue", attempted_at=now)
            db.add(snapshot)
            db.flush()
        with patch('apps.worker.services.monitoring._probes_for', side_effect=provider):
            _collect_kind(db, "queue", 900, now, deadline=time.monotonic() + 30,
                          storage_path=str(tmp_path), snapshot=snapshot)
    with Session(engine) as db:
        waiting = db.get(MonitoringMetrics, ("queue", "waiting"))
        assert waiting.value == 2 and waiting.last_attempt_at.replace(tzinfo=timezone.utc) == healthy_last
        failed = db.get(MonitoringMetrics, ("queue", "failed"))
        assert failed.value == 0 and failed.last_attempt_at.replace(tzinfo=timezone.utc) == now
        assert failed.error is None and failed.consecutive_failures == 0
