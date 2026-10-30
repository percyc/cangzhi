"""Exercise default-only migration without changing existing user settings."""
import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa


def test_default_migration_round_trip_preserves_saved_values():
    path = Path(__file__).resolve().parents[3] / "migrations/versions/0045_external_ocr_timeout_default.py"
    spec = importlib.util.spec_from_file_location("ocr_timeout_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE ai_runtime_configs (id INTEGER PRIMARY KEY, ocr_timeout_seconds INTEGER NOT NULL DEFAULT 30)")
        connection.exec_driver_sql("INSERT INTO ai_runtime_configs VALUES (1,30),(2,45),(3,120),(4,240)")
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
        assert connection.exec_driver_sql("SELECT ocr_timeout_seconds FROM ai_runtime_configs ORDER BY id").scalars().all() == [30,45,120,240]
        connection.exec_driver_sql("INSERT INTO ai_runtime_configs (id) VALUES (5)")
        assert connection.exec_driver_sql("SELECT ocr_timeout_seconds FROM ai_runtime_configs WHERE id=5").scalar() == 120
        with Operations.context(context):
            migration.downgrade()
        assert connection.exec_driver_sql("SELECT ocr_timeout_seconds FROM ai_runtime_configs ORDER BY id").scalars().all() == [30,45,120,240,120]
        connection.exec_driver_sql("INSERT INTO ai_runtime_configs (id) VALUES (6)")
        assert connection.exec_driver_sql("SELECT ocr_timeout_seconds FROM ai_runtime_configs WHERE id=6").scalar() == 30
