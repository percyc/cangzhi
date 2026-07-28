"""Shared pytest fixtures for the API test suite.

Authentication is exercised per-route. Tests that do not need to
prove the gate work inject a stub admin via
``app.dependency_overrides[require_admin]``; tests that explicitly
need the 401 path clear the override so the real dependency
fires.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from apps.api.api.auth import require_admin
from apps.api.core.db import Base, get_db
from apps.api.main import app
from apps.api.storage import get_storage
from apps.api.storage.local import LocalBlobStorage

# Ensure every mapped table is registered before create_all.
import apps.api.models  # noqa: F401, E402


@pytest.fixture
def client(tmp_path: Path):
    """Yield a TestClient with an in-memory database and a stub admin.

    The auth dependency is overridden with a stand-in ``Admin`` so
    routes protected by ``require_admin`` are reachable without
    going through the cookie login. Tests that need the real
    dependency can ``app.dependency_overrides.pop(require_admin)``
    and drive the cookie flow themselves.
    """

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def prepare_database():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def dispose_database():
        await engine.dispose()

    async def override_db():
        async with session_factory() as session:
            yield session

    storage = LocalBlobStorage(tmp_path / "storage")
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[require_admin] = lambda: _stub_admin()

    import asyncio

    asyncio.run(prepare_database())
    with TestClient(app) as test_client:
        yield test_client, storage
    app.dependency_overrides.clear()
    asyncio.run(dispose_database())


def _stub_admin():
    """Return a minimal stand-in for the Admin row.

    The stub uses ``SimpleNamespace`` so tests can reach in for
    ``id``/``username`` without committing a row. The real model
    is intentionally not imported here to keep this helper free of
    side effects on the test database.
    """

    from types import SimpleNamespace

    return SimpleNamespace(id=1, username="tester", is_active=True)
