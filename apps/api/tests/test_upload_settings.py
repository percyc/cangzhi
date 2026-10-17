import pytest

from apps.api.api.auth import require_admin
from apps.api.core.config import settings
from apps.api.main import app


def test_default_environment_fallback_and_runtime_persistence(client, monkeypatch):
    http, _ = client
    monkeypatch.setattr(settings, "max_upload_size_mb", 50)
    assert http.get('/api/system/upload-settings').json() == {"max_upload_size_mb": 50}
    monkeypatch.setattr(settings, "max_upload_size_mb", 60)
    assert http.get('/api/system/upload-settings').json()["max_upload_size_mb"] == 60
    assert http.put('/api/system/upload-settings', json={"max_upload_size_mb": 100}).status_code == 200
    assert http.get('/api/system/upload-settings').json()["max_upload_size_mb"] == 100
    assert http.put('/api/system/upload-settings', json={"max_upload_size_mb": 50}).status_code == 200
    assert http.get('/api/system/upload-settings').json()["max_upload_size_mb"] == 50


@pytest.mark.parametrize('value', [0, -1, 1.5, True, '100', None, 2147483648])
def test_invalid_values_leave_settings_unchanged(client, value):
    http, _ = client
    http.put('/api/system/upload-settings', json={"max_upload_size_mb": 20})
    assert http.put('/api/system/upload-settings', json={"max_upload_size_mb": value}).status_code == 422
    assert http.get('/api/system/upload-settings').json()["max_upload_size_mb"] == 20


def test_setting_requires_admin(client):
    http, _ = client
    app.dependency_overrides.pop(require_admin)
    assert http.get('/api/system/upload-settings').status_code == 401
    assert http.put('/api/system/upload-settings', json={"max_upload_size_mb": 200}).status_code == 401


def test_runtime_limit_enforced_and_adjustable_without_restart(client):
    http, storage = client
    http.put('/api/system/upload-settings', json={"max_upload_size_mb": 1})
    payload = b'x' * (1024 * 1024 + 1)
    assert http.post('/api/files/upload', files={'file': ('large.txt', payload, 'text/plain')}).status_code == 413
    http.put('/api/system/upload-settings', json={"max_upload_size_mb": 2})
    result = http.post('/api/files/upload', files={'file': ('large.txt', payload, 'text/plain')})
    assert result.status_code == 201
    http.put('/api/system/upload-settings', json={"max_upload_size_mb": 1})
    assert http.post('/api/files/upload', files={'file': ('again.txt', payload, 'text/plain')}).status_code == 413
    # Lowering the cap does not remove a previously accepted document.
    assert http.get(f'/api/documents/{result.json()["id"]}').status_code == 200


def test_public_upload_uses_same_limit_but_cannot_change_it(client):
    from apps.api.tests.test_knowledge_v1 import _enable_real_login, _setup_owner
    http, _ = client
    _enable_real_login()
    _setup_owner(http)
    workspace_id = http.get('/api/workspaces/current').json()['id']
    assert http.put('/api/system/upload-settings', json={'max_upload_size_mb': 1}).status_code == 200
    token = http.post('/api/access-tokens', json={
        'name': 'synthetic uploader', 'scopes': ['documents:write'], 'workspace_id': workspace_id,
    }).json()['token']
    http.cookies.clear()
    headers = {'Authorization': f'Bearer {token}'}
    assert http.put('/api/system/upload-settings', headers=headers,
                    json={'max_upload_size_mb': 20}).status_code == 401
    assert http.post('/api/v1/documents', headers=headers,
                     files={'file': ('large.txt', b'x' * (1024 * 1024 + 1), 'text/plain')}).status_code == 413
    assert http.post('/api/v1/documents', headers=headers,
                     files={'file': ('fits.txt', b'x' * (1024 * 1024), 'text/plain')}).status_code == 202
