from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from apps.api.models.monitoring import MonitoringHistory, MonitoringSnapshot
from apps.worker.services.monitoring import collect_once, collect_capacity, collect_knowledge, collect_queue


@pytest.fixture
def engine():
    engine = create_engine("sqlite://")
    MonitoringSnapshot.__table__.create(engine)
    MonitoringHistory.__table__.create(engine)
    yield engine
    engine.dispose()


def test_frequency_history_and_retention(engine, tmp_path):
    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    capacity = {"blob_bytes": 100, "disk": {"used": 200}}
    with patch('apps.worker.services.monitoring.collect_queue', return_value={"waiting": 2}) as queue, \
         patch('apps.worker.services.monitoring.collect_knowledge', return_value={"documents": 3}) as knowledge, \
         patch('apps.worker.services.monitoring.collect_capacity', return_value=capacity):
        collect_once(engine, tmp_path, now=now)
        collect_once(engine, tmp_path, now=now + timedelta(seconds=30))
        assert queue.call_count == knowledge.call_count == 1
        collect_once(engine, tmp_path, now=now + timedelta(seconds=61))
        assert queue.call_count == 2 and knowledge.call_count == 1
        with Session(engine) as db:
            assert db.query(MonitoringHistory).count() == 1
        collect_once(engine, tmp_path, now=now + timedelta(days=32))
        with Session(engine) as db:
            assert db.query(MonitoringHistory).count() == 1


def test_failure_preserves_last_good_and_sanitizes(engine, tmp_path):
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        db.add(MonitoringSnapshot(kind="queue", data={"waiting": 5}, sampled_at=now - timedelta(hours=1), attempted_at=now - timedelta(hours=1)))
        db.commit()
    with patch('apps.worker.services.monitoring.collect_queue', side_effect=RuntimeError('secret-password')), \
         patch('apps.worker.services.monitoring.collect_knowledge', return_value={"documents": 0}), \
         patch('apps.worker.services.monitoring.collect_capacity', return_value={"blob_bytes": 0, "disk": None}):
        collect_once(engine, tmp_path, now=now)
    with Session(engine) as db:
        row = db.get(MonitoringSnapshot, 'queue')
        assert row.data == {"waiting": 5}
        assert 'secret' not in row.error
        assert row.sampled_at.replace(tzinfo=timezone.utc) == now - timedelta(hours=1)


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
        q = collect_queue(db)
        assert q['waiting'] == 1 and q['failed'] == 0
        c = collect_capacity(db, tmp_path)
        assert c['blob_bytes'] == 170 and c['original_bytes'] == 100
        assert c['preview_bytes'] == 50 and c['other_blob_bytes'] == 20
        assert c['parquet_bytes'] == 10 and c['database_bytes'] is None
        assert c['disk']['total'] > 0
        with patch('shutil.disk_usage', side_effect=OSError):
            assert collect_capacity(db, tmp_path)['disk'] is None
