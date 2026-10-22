"""Independent release checks for retained operational measurements."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api.models.monitoring import MonitoringHistory, MonitoringMetrics, MonitoringSnapshot
from apps.worker.services import monitoring


def test_existing_hour_point_updates_and_never_moves_an_old_sample_forward(tmp_path):
    engine = create_engine("sqlite://")
    for table in (MonitoringSnapshot, MonitoringMetrics, MonitoringHistory):
        table.__table__.create(engine)
    now = datetime(2026, 10, 23, 0, 1, tzinfo=timezone.utc)
    values = {"documents": 1, "blob_bytes": 100}

    def probes(kind, storage_path):
        if kind == "knowledge":
            return [(("documents",), lambda db: {"documents": values["documents"]})]
        if kind == "capacity":
            return [(("blob_bytes",), lambda db: {"blob_bytes": values["blob_bytes"]})]
        return []

    try:
        with patch.object(monitoring, "_probes_for", side_effect=probes):
            monitoring.collect_once(engine, tmp_path, now=now)
            values.update(documents=2, blob_bytes=200)
            monitoring.collect_once(engine, tmp_path, now=now + timedelta(minutes=16))
        with Session(engine) as db:
            points = db.query(MonitoringHistory).all()
            assert len(points) == 1
            assert points[0].data["blob_bytes"] == 200
            assert points[0].data["documents"] == 2
        with patch.object(monitoring, "_probes_for", return_value=[]):
            monitoring.collect_once(engine, tmp_path, now=now + timedelta(hours=1))
        with Session(engine) as db:
            assert db.query(MonitoringHistory).count() == 1
    finally:
        engine.dispose()


def test_backoff_remains_bounded_even_after_many_failures():
    with patch.object(monitoring.random, "uniform", return_value=1.2):
        for failures in (1, 10, 10000):
            delay = monitoring._backoff_seconds(failures)
            assert monitoring.BACKOFF_MIN_S <= delay <= monitoring.BACKOFF_MAX_S
        assert monitoring._backoff_seconds(10000) == monitoring.BACKOFF_MAX_S
