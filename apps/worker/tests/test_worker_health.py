from apps.worker.core.config import settings
from apps.worker.healthcheck import check_database


def test_settings_load():
    assert settings.database_url
    assert settings.poll_interval > 0


def test_database_healthcheck_with_sqlite():
    check_database("sqlite+pysqlite:///:memory:")


def test_storage_path_configured():
    assert settings.storage_path
