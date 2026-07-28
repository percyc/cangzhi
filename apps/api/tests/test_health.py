from fastapi.testclient import TestClient
from apps.api.main import app

client = TestClient(app)


def test_liveness_check():
    """Test liveness check always returns 200 when app is running"""
    response = client.get("/api/liveness")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


def test_root_endpoint():
    """Test root endpoint returns correct info"""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["service"] == "cangzhi-api"
    assert data["status"] == "ok"
