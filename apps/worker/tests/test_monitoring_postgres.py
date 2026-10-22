"""Explicit isolated migrated PostgreSQL only; never default to runtime DB."""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from apps.worker.services.monitoring import collect_once
from apps.api.models.monitoring import MonitoringSnapshot, MonitoringHistory


def test_real_pg_aggregation_lock_timeout_and_retry(tmp_path):
    url = os.getenv('CANGZHI_MONITOR_TEST_DATABASE_URL')
    if not url:
        pytest.skip('explicit isolated database required')
    assert url.endswith('/cangzhi_test')
    engine = create_engine(url)
    now = datetime.now(timezone.utc)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(collect_once, engine, str(tmp_path), now=now) for _ in range(2)]
            for future in results:
                future.result()
        with Session(engine) as db:
            assert db.query(MonitoringSnapshot).count() == 3
            assert db.query(MonitoringHistory).count() == 1
            assert all(row.error is None for row in db.query(MonitoringSnapshot))
            assert db.get(MonitoringSnapshot, 'capacity').data['database_bytes'] > 0
        def slow(db):
            db.execute(text('SELECT pg_sleep(5)'))
        started = time.monotonic()
        with patch('apps.worker.services.monitoring.collect_queue', slow):
            collect_once(engine, str(tmp_path), now=now + timedelta(seconds=61))
        assert time.monotonic() - started < 4.5
        with Session(engine) as db:
            row = db.get(MonitoringSnapshot, 'queue')
            assert row.error and row.data['waiting'] == 0
            assert row.sampled_at == now
        collect_once(engine, str(tmp_path), now=now + timedelta(seconds=122))
        with Session(engine) as db:
            assert db.get(MonitoringSnapshot, 'queue').error is None
    finally:
        engine.dispose()
