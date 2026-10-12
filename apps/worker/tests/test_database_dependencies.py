"""Guard the driver contract used by offline images (no database needed)."""

from pathlib import Path

import pytest
from sqlalchemy import create_engine, engine_from_config
from sqlalchemy.ext.asyncio import create_async_engine

from apps.api.core.db import _resolve_async_url


@pytest.mark.parametrize("service", ["api", "worker"])
def test_requirements_keep_sqlalchemy_on_psycopg2_default(service):
    root = Path(__file__).resolve().parents[3]
    requirements = [
        line.strip()
        for line in (root / "apps" / service / "requirements.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    sqlalchemy = [item for item in requirements if item.lower().startswith("sqlalchemy")]
    assert sqlalchemy == ["sqlalchemy>=2.0,<2.1"]
    assert "psycopg2-binary" in requirements


@pytest.mark.parametrize("migration", [False, True])
def test_sync_driver_loads_without_psycopg3(migration):
    url = "postgresql://test@127.0.0.1/test"
    engine = (
        engine_from_config({"sqlalchemy.url": url}, prefix="sqlalchemy.")
        if migration else create_engine(url)
    )
    try:
        assert engine.dialect.driver == "psycopg2"
        assert engine.dialect.dbapi.__name__ == "psycopg2"
    finally:
        engine.dispose()


@pytest.mark.parametrize("scheme", ["postgresql", "postgres"])
def test_api_keeps_explicit_async_driver(scheme):
    engine = create_async_engine(_resolve_async_url(f"{scheme}://test@127.0.0.1/test"))
    assert engine.dialect.driver == "asyncpg"
    # No connection was opened; disposing the synchronous pool is sufficient.
    engine.sync_engine.dispose()
