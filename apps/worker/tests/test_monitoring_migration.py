"""Migration 0041 upgrade/downgrade coverage for preserved old snapshot data.

Exercises the real 0041 ``upgrade()``/``downgrade()`` functions through alembic's
``Operations`` proxy against a scratch engine, so it does not depend on the alembic
revision graph (which the lease batch can leave temporarily unresolvable while a
sibling revision such as 0042 is being prepared). The old capacity snapshot carries
a nested ``disk`` dict and a sanitized legacy ``error`` to prove the typed JSON insert
and error-category preservation work. Uses an isolated scratch PostgreSQL database
when ``CANGZHI_MONITOR_TEST_DATABASE_URL`` is set, otherwise an in-memory SQLite.
"""
import importlib.util
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from apps.api.models.monitoring import MonitoringHistory, MonitoringSnapshot
from apps.api.services.monitoring import aware

_MIGRATION_REL = Path("migrations/versions/0041_monitoring_metric_state.py")


def _root() -> Path:
    # /app/apps/worker/tests/... -> repo root (holds apps/ and migrations/).
    return Path(__file__).resolve().parents[3]


def _load_0041():
    path = _root() / _MIGRATION_REL
    if not path.exists():
        pytest.skip("0041 migration module not available on the mounted tree")
    spec = importlib.util.spec_from_file_location("_m0041", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pg_scratch(url):
    """Create a throwaway database on the same PG server; never touches cangzhi_test."""
    from sqlalchemy.engine import make_url
    parsed = make_url(url)
    name = "cz_monitor_test_" + uuid.uuid4().hex
    admin_url = parsed.set(database="postgres")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception:  # noqa: BLE001 - missing CREATEDB rights on the shared instance
        admin.dispose()
        pytest.skip("no CREATE DATABASE rights to build an isolated migration scratch DB")
    admin.dispose()
    scratch = create_engine(parsed.set(database=name))
    return scratch


def _engine():
    url = os.getenv("CANGZHI_MONITOR_TEST_DATABASE_URL")
    if url:
        assert url.endswith("/cangzhi_test")
        return _pg_scratch(url)
    return create_engine("sqlite://")


def _seed_old_snapshots(engine):
    old = _datetimes()
    with Session(engine) as db, db.begin():
        db.add(MonitoringSnapshot(kind="capacity",
                                  data={"disk": {"total": 1000, "used": 600, "free": 400},
                                        "blob_bytes": 50, "original_bytes": 40,
                                        "preview_bytes": 5, "other_blob_bytes": 5,
                                        "parquet_bytes": 10, "database_bytes": 500,
                                        "vector_table_bytes": 20, "blobs": 3},
                                  sampled_at=old["sampled"], attempted_at=old["attempted"],
                                  error="旧快照映射：最后一次采集失败，当前显示旧值。"))
        db.add(MonitoringSnapshot(kind="queue", data={"waiting": 7, "running": 1, "failed": 2},
                                  sampled_at=old["sampled"], attempted_at=old["attempted"]))
        db.add(MonitoringSnapshot(kind="knowledge", data={"documents": 3, "vectors": 9},
                                  sampled_at=old["sampled"], attempted_at=old["attempted"]))


def _datetimes():
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 10, 23, 3, 4, 5, tzinfo=timezone.utc)
    return {"sampled": now - timedelta(hours=2),
            "attempted": now - timedelta(minutes=1)}


def test_0041_upgrade_backfills_old_dict_with_error_and_downgrade_drops():
    scratch = _engine()
    mod = _load_0041()
    try:
        MonitoringSnapshot.__table__.create(scratch)
        MonitoringHistory.__table__.create(scratch)
        _seed_old_snapshots(scratch)

        _run_migration(scratch, mod.upgrade)

        with Session(scratch) as db:
            from apps.api.models.monitoring import MonitoringMetrics
            cap_disk = db.get(MonitoringMetrics, ("capacity", "disk"))
            assert cap_disk is not None
            assert cap_disk.value == {"total": 1000, "used": 600, "free": 400}
            assert cap_disk.error == mod.LEGACY_ERROR  # sanitized category preserved
            cap_blob = db.get(MonitoringMetrics, ("capacity", "blob_bytes"))
            assert cap_blob.value == 50 and cap_blob.error == mod.LEGACY_ERROR
            queue = db.get(MonitoringMetrics, ("queue", "waiting"))
            assert queue.value == 7 and queue.error is None  # no prior error -> ok
            knowledge = db.get(MonitoringMetrics, ("knowledge", "documents"))
            assert knowledge.value == 3
            # timestamps are carried, not re-stamped as fresh.
            assert aware(cap_disk.last_success_at) == _datetimes()["sampled"]
            assert aware(cap_disk.last_attempt_at) == _datetimes()["attempted"]

        _run_migration(scratch, mod.downgrade)
        insp = inspect(scratch)
        assert "monitoring_metrics" not in insp.get_table_names()
        # Source snapshots are untouched by upgrade/downgrade.
        with Session(scratch) as db:
            snap = db.get(MonitoringSnapshot, "capacity")
            assert snap.data["disk"]["used"] == 600
            assert snap.data["blob_bytes"] == 50
    finally:
        _teardown(scratch)


def _run_migration(engine, fn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            fn()


def _teardown(engine):
    url = engine.url
    name = url.database or ""
    if name.startswith("cz_monitor_test_") and len(name) == len("cz_monitor_test_") + 32:
        engine.dispose()
        admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE "{name}"'))
        finally:
            admin.dispose()
    else:
        engine.dispose()
