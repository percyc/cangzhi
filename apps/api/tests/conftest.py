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

# Ensure every mapped table is registered before create_all.
import apps.api.models  # noqa: F401
from apps.api.api.auth import require_admin
from apps.api.core.db import Base, get_db
from apps.api.main import app
from apps.api.models.workspaces import DEFAULT_WORKSPACE_SLUG, Workspace
from apps.api.storage import get_storage
from apps.api.storage.local import LocalBlobStorage


@pytest.fixture(autouse=True)
def _isolated_execution_and_admission(monkeypatch):
    """Keep worker direct-calls explicit and give every API test a safe,
    in-memory model-admission backend (test-only; production never fails open).

    The in-memory backend mirrors the Postgres lease/CAS/cooldown semantics and
    is gated on the test interpreter, exactly like the worker test fixture.
    """
    from apps.api.services import model_admission as admission
    from apps.worker.core.config import settings as worker_settings

    monkeypatch.setattr(worker_settings, "task_execution_enabled", False)
    admission.configure_admission_backend(admission.InMemoryAdmissionBackend())
    admission.set_process_role("foreground")
    yield
    admission.reset_admission_backend()
    admission.set_process_role("foreground")


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
        async with session_factory() as session:
            session.add(
                Workspace(
                    slug=DEFAULT_WORKSPACE_SLUG,
                    name="默认空间",
                    is_default=True,
                    status="active",
                    settings={},
                )
            )
            await session.commit()

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
