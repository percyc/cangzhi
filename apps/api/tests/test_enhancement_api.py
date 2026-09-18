"""Admin routes: explicit consent, input validation and workspace isolation."""
import asyncio

import pytest

from apps.api.api.auth import require_admin
from apps.api.core.db import get_db
from apps.api.main import app
from apps.api.models.documents import DocumentVersion
from apps.api.models.workspaces import DEFAULT_WORKSPACE_SLUG


def prepared_note(client):
    test_client, _ = client
    doc = test_client.post('/api/notes', json={'title': 'Test', 'content': 'Facts'}).json()

    async def prepare():
        async for db in app.dependency_overrides[get_db]():
            from sqlalchemy import select
            version = await db.scalar(select(DocumentVersion).where(DocumentVersion.document_id == doc['id']))
            version.structured_content = {'document_type': 'note', 'blocks': [
                {'type': 'paragraph', 'text': 'Facts', 'heading_path': []}]}
            await db.commit()
    asyncio.run(prepare())
    return doc['id']


def test_management_requires_admin(client):
    test_client, _ = client
    app.dependency_overrides.pop(require_admin)
    for method, path in [('GET', '/api/enhancement/settings'), ('PUT', '/api/enhancement/settings'),
                         ('GET', '/api/documents/1/enhancements'), ('POST', '/api/documents/1/enhancements'),
                         ('GET', '/api/enhancements/1'), ('POST', '/api/enhancements/1/cancel'),
                         ('POST', '/api/enhancements/1/resume')]:
        assert test_client.request(method, path, json={}).status_code == 401


@pytest.mark.parametrize('change', [{'call_budget': True}, {'call_budget': 33},
    {'modules': ['chunking']}, {'effective_at': '2020-01-01'}, {'cost_acknowledged': False}])
def test_settings_reject_invalid_and_unconsented(client, change):
    test_client, _ = client
    body = {'enabled': True, 'modules': ['chapter'], 'call_budget': 2, 'cost_acknowledged': True}
    assert test_client.put('/api/enhancement/settings', json={**body, **change}).status_code == 422
    assert not test_client.get('/api/enhancement/settings').json()['enabled']


def test_roundtrip_settings_preserved_and_scoped_runs(client):
    test_client, _ = client
    settings = {'enabled': True, 'modules': ['chapter', 'graph'], 'call_budget': 2, 'cost_acknowledged': True}
    configured = test_client.put('/api/enhancement/settings', json=settings)
    assert configured.status_code == 200
    assert configured.json()['effective_at']
    url = f'/api/workspaces/{DEFAULT_WORKSPACE_SLUG}'
    assert test_client.patch(url, json={'settings': {'knowledge_enhancement': {}}}).status_code == 400
    assert test_client.patch(url, json={'settings': {'theme': 'dark'}}).status_code == 200
    assert test_client.get('/api/enhancement/settings').json() == configured.json()
    doc_id = prepared_note(client)
    create_url = f'/api/documents/{doc_id}/enhancements'
    assert test_client.post(create_url, json={}).status_code == 422
    created = test_client.post(create_url, json={'cost_acknowledged': True})
    assert created.status_code == 200, created.text
    run_id = created.json()['id']
    assert test_client.post(create_url, json={'cost_acknowledged': True}).json()['id'] == run_id
    detail_url = f'/api/enhancements/{run_id}'
    assert test_client.get(detail_url).json()['windows'][0]['source_segments'][0]['text'] == 'Facts'
    assert test_client.get(detail_url + '?limit=11').status_code == 422
    assert test_client.post('/api/workspaces', json={'slug': 'other', 'name': 'Other'}).status_code == 201
    headers = {'X-Cangzhi-Workspace': 'other'}
    assert not test_client.get('/api/enhancement/settings', headers=headers).json()['enabled']
    for method, path in [('GET', create_url), ('GET', detail_url), ('POST', detail_url + '/cancel'),
                         ('POST', detail_url + '/resume')]:
        assert test_client.request(method, path, headers=headers,
            json={'additional_calls': 1, 'cost_acknowledged': True}).status_code == 404
    assert test_client.post(detail_url + '/cancel').json()['status'] == 'cancelled'
    assert test_client.post(detail_url + '/resume', json={'additional_calls': 1}).status_code == 422
    assert test_client.post(detail_url + '/resume', json={
        'additional_calls': 1, 'cost_acknowledged': True}).json()['status'] == 'queued'
