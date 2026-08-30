from __future__ import annotations

from collections import namedtuple

from apps.api.api import system


def test_system_status_is_admin_only(client):
    test_client, _storage = client
    from apps.api.api.auth import require_admin
    from apps.api.main import app

    app.dependency_overrides.pop(require_admin)
    response = test_client.get("/api/system/status")
    assert response.status_code == 401


def test_system_status_reports_actionable_service_metrics(client, monkeypatch):
    test_client, _storage = client
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(system.shutil, "disk_usage", lambda _path: usage(1000, 250, 750))

    response = test_client.get("/api/system/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["uptime_seconds"] >= 0
    assert payload["database"]["status"] == "ok"
    assert payload["database"]["latency_ms"] >= 0
    assert payload["storage"] == {
        "status": "ok",
        "total_bytes": 1000,
        "used_bytes": 250,
        "free_bytes": 750,
        "used_percent": 25.0,
    }
    assert payload["processing"] == {"active": 0, "waiting": 0, "failed": 0}
