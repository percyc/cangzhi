from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from apps.api.core.db import Base, get_db
from apps.api.main import app
from apps.api.storage import get_storage
from apps.api.storage.local import LocalBlobStorage

# Ensure every mapped table is registered before create_all.
import apps.api.models  # noqa: F401, E402


@pytest.fixture
def client(tmp_path: Path):
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

    import asyncio

    asyncio.run(prepare_database())
    with TestClient(app) as test_client:
        yield test_client, storage
    app.dependency_overrides.clear()
    asyncio.run(dispose_database())
