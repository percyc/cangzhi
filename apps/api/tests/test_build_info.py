from apps.api.api.auth import require_admin
from apps.api.core.build_info import build_info
from apps.api.main import app


def test_version_endpoint_reports_only_image_metadata(client, monkeypatch):
    test_client, _ = client
    expected = {"version": "v1.3.2", "revision": "a" * 40,
        "built_at": "2026-10-18T00:00:00Z", "dirty": False}
    for name, value in {"VERSION": expected["version"], "REVISION": expected["revision"],
        "BUILD_TIME": expected["built_at"], "BUILD_DIRTY": "false"}.items():
        monkeypatch.setenv(f"CANGZHI_{name}", value)
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-be-disclosed")
    assert test_client.get("/api/system/version").json() == expected
    app.dependency_overrides.pop(require_admin, None)
    assert test_client.get("/api/system/version").status_code == 401


def test_missing_provenance_never_claims_current_release(monkeypatch):
    for name in ("VERSION", "REVISION", "BUILD_TIME", "BUILD_DIRTY"):
        monkeypatch.delenv(f"CANGZHI_{name}", raising=False)
    assert build_info() == {"version": "unversioned", "revision": "unknown", "built_at": "unknown", "dirty": True}
