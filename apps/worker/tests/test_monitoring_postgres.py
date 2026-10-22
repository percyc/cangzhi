"""Explicit isolated migrated PostgreSQL only; never default to runtime DB."""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from apps.worker.services.monitoring import collect_once, ACTIVE_VECTOR_COUNT_SQL
from apps.api.models.monitoring import MonitoringSnapshot, MonitoringMetrics, MonitoringHistory


def test_real_pg_aggregation_lock_timeout_and_retry(tmp_path):
    url = os.getenv('CANGZHI_MONITOR_TEST_DATABASE_URL')
    if not url:
        pytest.skip('explicit isolated database required')
    assert url.endswith('/cangzhi_test')
    engine = create_engine(url)
    now = datetime.now(timezone.utc)
    try:
        # This explicitly isolated fixture must be repeatable across hours and
        # reruns; stale synthetic history is not a collector duplicate.
        with engine.begin() as db:
            for table in ("monitoring_history", "monitoring_metrics", "monitoring_snapshots"):
                db.execute(text(f"DELETE FROM {table}"))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(collect_once, engine, str(tmp_path), now=now) for _ in range(2)]
            for future in results:
                future.result()
        with Session(engine) as db:
            assert db.query(MonitoringSnapshot).count() == 3
            assert db.query(MonitoringHistory).count() == 1
            assert all(row.error is None for row in db.query(MonitoringSnapshot))
            assert isinstance(db.get(MonitoringSnapshot, 'queue').data['model_pools'], list)
            assert db.get(MonitoringSnapshot, 'queue').data['quarantined'] >= 0
            assert db.get(MonitoringSnapshot, 'queue').data['waiting_model'] >= 0
            assert db.get(MonitoringSnapshot, 'queue').data['recoveries'] >= 0
            before_waiting = db.get(MonitoringSnapshot, 'queue').data['waiting']
            assert db.get(MonitoringSnapshot, 'capacity').data['database_bytes'] > 0

        def slow(db):
            db.execute(text('SELECT pg_sleep(5)'))

        # The collector no longer calls collect_queue directly; it runs the
        # ``_queue_states`` probe. Fixing random jitter to 1.0 makes the
        # bounded first backoff exactly BACKOFF_MIN_S so recovery is deterministic.
        started = time.monotonic()
        with patch('apps.worker.services.monitoring.random.uniform', return_value=1.0), \
             patch('apps.worker.services.monitoring._queue_states', slow):
            collect_once(engine, str(tmp_path), now=now + timedelta(seconds=61))
        assert time.monotonic() - started < 4.5
        first_attempt = now + timedelta(seconds=61)
        with Session(engine) as db:
            row = db.get(MonitoringSnapshot, 'queue')
            assert row.error  # partial group failure surfaced
            assert row.data['waiting'] == before_waiting  # old value retained
            # A healthy sibling metric persisted alongside the timed-out one.
            oldest = db.get(MonitoringMetrics, ('queue', 'oldest_waiting_at'))
            assert oldest is not None and oldest.error is None
            states = db.get(MonitoringMetrics, ('queue', 'failed'))
            assert states.error and states.last_success_at is not None
            assert states.next_attempt_at > states.last_attempt_at  # bounded backoff
            # sampled_at reflects the newly-successful healthy metric, not the stale one.
            assert row.sampled_at.replace(microsecond=0) == first_attempt.replace(microsecond=0)

        # With BACKOFF_MIN_S=60 fixed, next_attempt = first_attempt+60 <= now+122 -> due.
        collect_once(engine, str(tmp_path), now=now + timedelta(seconds=122))
        with Session(engine) as db:
            assert db.get(MonitoringSnapshot, 'queue').error is None
            assert db.get(MonitoringMetrics, ('queue', 'failed')).error is None
    finally:
        engine.dispose()


def test_active_vector_count_correlated_hashes_at_scale():
    """50k unique matching hashes must not cause repeated full document scans.

    Temporary, metadata-only tables; never loads vectors or user data. The
    timeout is the production collector budget, not a microbenchmark claim.
    """
    url = os.getenv('CANGZHI_MONITOR_TEST_DATABASE_URL')
    if not url:
        pytest.skip('explicit isolated database required')
    assert url.endswith('/cangzhi_test')
    engine = create_engine(url)
    try:
        with engine.begin() as db:
            for ddl in [
                'CREATE TEMP TABLE documents(id int PRIMARY KEY, current_version_id int, is_deleted boolean)',
                'CREATE TEMP TABLE embedding_profiles(id int PRIMARY KEY, status text)',
                'CREATE TEMP TABLE document_chunks(id int PRIMARY KEY, document_version_id int, is_current boolean, role text, content_hash text)',
                'CREATE TEMP TABLE chunk_embeddings(chunk_id int PRIMARY KEY, profile_id int, content_hash text)',
                'CREATE INDEX ON chunk_embeddings(profile_id)',
                "INSERT INTO embedding_profiles VALUES(1,'active'),(2,'retired')",
                'INSERT INTO documents SELECT i,i,false FROM generate_series(1,2000) i',
                "INSERT INTO document_chunks SELECT i,(i-1)/25+1,true,'child',md5(i::text) FROM generate_series(1,50000) i",
                "INSERT INTO chunk_embeddings SELECT i,1,md5(i::text) FROM generate_series(1,50000) i",
                'ANALYZE documents', 'ANALYZE document_chunks', 'ANALYZE chunk_embeddings', 'ANALYZE embedding_profiles',
            ]:
                db.execute(text(ddl))
            db.execute(text("SET LOCAL statement_timeout='2500ms'"))
            db.execute(text('SET LOCAL max_parallel_workers_per_gather=0'))
            assert db.scalar(text(ACTIVE_VECTOR_COUNT_SQL)) == 50000
            db.execute(text("UPDATE chunk_embeddings SET content_hash='stale' WHERE chunk_id=1"))
            db.execute(text('UPDATE chunk_embeddings SET content_hash=NULL WHERE chunk_id=2'))
            db.execute(text('UPDATE chunk_embeddings SET profile_id=2 WHERE chunk_id=3'))
            db.execute(text("UPDATE document_chunks SET role='parent' WHERE id=4"))
            db.execute(text('UPDATE document_chunks SET is_current=false WHERE id=5'))
            db.execute(text('UPDATE documents SET is_deleted=true WHERE id=2000'))
            db.execute(text('UPDATE documents SET current_version_id=99999 WHERE id=1999'))
            assert db.scalar(text(ACTIVE_VECTOR_COUNT_SQL)) == 49945
    finally:
        engine.dispose()
