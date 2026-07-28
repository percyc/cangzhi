from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession
from unittest.mock import Mock
import pytest

from apps.api.main import app
from apps.api.core.db import get_db


client = TestClient(app)


@pytest.fixture(autouse=True)
def clear_dependency_overrides():
    yield
    app.dependency_overrides.clear()


async def mock_get_db_working():
    mock_session = Mock(spec=AsyncSession)
    yield mock_session


async def mock_get_db_down():
    mock_session = Mock(spec=AsyncSession)
    mock_session.execute.side_effect = Exception("Database connection failed")
    yield mock_session


def test_readiness_ok():
    """Readiness returns 200 when database is available"""
    # Override dependency to bypass actual database connection
    app.dependency_overrides[get_db] = mock_get_db_working
    response = client.get("/api/readiness")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["database"] == "ok"


def test_readiness_down():
    """Readiness returns 503 when database is not available"""
    app.dependency_overrides[get_db] = mock_get_db_down
    response = client.get("/api/readiness")
    assert response.status_code == 503
