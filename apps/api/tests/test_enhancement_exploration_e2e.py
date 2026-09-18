"""Real REST/MCP authentication through shared enhancement reads; no model calls."""
import asyncio
import json

from sqlalchemy import delete, select, func

from apps.api.api.auth import require_admin, reset_auth_limiters_for_tests
from apps.api.core.db import get_db
from apps.api.main import app
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.document_scope_keys import DocumentScopeKey
from apps.api.models.enhancement import EnhancementRun, EnhancementWindow
from apps.api.models.processing import ProcessingJob
from apps.api.services import knowledge_enhancement as service
from apps.api.services.workspaces import bind_workspace_context


def test_real_rest_mcp_grant_boundary_and_evidence_parity(client, monkeypatch):
    test_client, _ = client
    app.dependency_overrides.pop(require_admin, None)
    reset_auth_limiters_for_tests()
    assert test_client.post('/api/auth/setup', json={
        'username': 'owner', 'password': 'a-strong-password'}).status_code == 200
    space = test_client.get('/api/workspaces/current').json()
    other = test_client.post('/api/workspaces', json={'slug': 'other', 'name': 'Other'}).json()

    async def seed():
        async for db in app.dependency_overrides[get_db]():
            data = []
            for index, workspace_id in enumerate([space['id'], space['id'], other['id']]):
                bind_workspace_context(db.sync_session, workspace_id)
                await db.run_sync(lambda s: service.save_settings(s, {'enabled': True,
                    'modules': ['chapter', 'graph'], 'call_budget': 1, 'cost_acknowledged': True}))
                doc = Document(title=f'Sample {index}', workspace_id=workspace_id, source_type=DocumentSourceType.note)
                db.add(doc)
                await db.flush()
                version = DocumentVersion(document_id=doc.id, version_number=1, content_hash=str(index),
                    processing_status='ready', raw_content='原文证据', structured_content={
                        'document_type': 'note', 'blocks': [{'type': 'paragraph', 'text': '原文证据', 'heading_path': []}]})
                db.add(version)
                await db.flush()
                doc.current_version_id = version.id
                await db.flush()
                created = await db.run_sync(lambda s: service.start_run(s, doc.id, cost_acknowledged=True))
                run = await db.get(EnhancementRun, created['id'])
                run.status, run.calls_used = 'completed', 1
                window = await db.scalar(select(EnhancementWindow).where(EnhancementWindow.run_id == run.id))
                window.status = 'completed'
                window.result = {'summary': {'text': '摘要解释', 'evidence_ids': [0]},
                    'entities': [{'id': 'e1', 'name': '主体', 'kind': 'entity', 'aliases': [], 'evidence_ids': [0]}],
                    'relations': [{'subject': 'e1', 'object': 'e1', 'predicate': '关系', 'evidence_ids': [0]}],
                    'events': [{'text': '事件解释', 'evidence_ids': [0]}],
                    'evidence_status': 'model_extracted_unverified'}
                if index == 0:
                    db.add(DocumentScopeKey(workspace_id=workspace_id, document_id=doc.id, scope_key='sample-group'))
                data.append((doc.id, run.id))
            await db.commit()
            return data
    pairs = asyncio.run(seed())
    token = test_client.post('/api/access-tokens', json={'name': 'read-test',
        'scopes': ['knowledge:read'], 'workspace_id': space['id']}).json()['token']
    no_read_token = test_client.post('/api/access-tokens', json={'name': 'search-test',
        'scopes': ['knowledge:search'], 'workspace_id': space['id']}).json()['token']
    test_client.cookies.clear()
    pat_headers = {'Authorization': f'Bearer {token}'}
    created_grant = test_client.post('/api/v1/exploration-grants', headers=pat_headers,
        json={'document_selection': {'scope_keys': ['sample-group']}, 'ttl_seconds': 600})
    assert created_grant.status_code == 201
    grant_headers = {'Authorization': 'Bearer ' + created_grant.json()['token']}

    def mcp(name, args, headers=grant_headers):
        response = test_client.post('/api/mcp', headers=headers, json={
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}})
        assert response.status_code == 200
        return response.json()['result']

    def no_model(*args, **kwargs):
        raise AssertionError('Read must not instantiate or call model')
    monkeypatch.setattr('apps.api.api.v1.build_provider_from_db', no_model)
    monkeypatch.setattr('apps.api.api.mcp.build_provider_from_db', no_model)
    monkeypatch.setattr('apps.worker.services.enhancement_processor.build_provider_from_session', no_model)
    doc_id, run_id = pairs[0]
    listing_url = f'/api/v1/knowledge/documents/{doc_id}/enhancements'
    read_url = f'/api/v1/knowledge/enhancements/{run_id}'
    listing = test_client.get(listing_url, headers=grant_headers)
    assert listing.status_code == 200
    assert listing.json() == mcp('knowledge_list_enhancements', {'document_id': doc_id})['structuredContent']
    for view in ['summary', 'entities', 'relations', 'events', 'evidence']:
        result = test_client.get(read_url, params={'view': view}, headers=grant_headers)
        assert result.status_code == 200
        assert result.json() == mcp('knowledge_get_enhancement', {'run_id': run_id, 'view': view})['structuredContent']
        assert result.json()['items']
        if view == 'summary':
            assert result.json()['items'][0] == {'text': '摘要解释', 'evidence_ids': [0]}
        if view == 'evidence':
            assert result.json()['items'][0]['id'] == 0
            assert result.json()['items'][0]['text'] == '原文证据'
        serialized = result.text
        assert 'scope_keys' not in serialized and 'source_snapshot' not in serialized
        assert result.json()['run']['calls_used'] == 1

    for outside_doc, outside_run in pairs[1:]:
        assert test_client.get(f'/api/v1/knowledge/documents/{outside_doc}/enhancements', headers=grant_headers).status_code == 404
        assert test_client.get(f'/api/v1/knowledge/enhancements/{outside_run}', headers=grant_headers,
            params={'document_ids': outside_doc}).status_code == 404
        escaped = mcp('knowledge_get_enhancement', {'run_id': outside_run,
            'document_selection': {'document_ids': [outside_doc]}})
        assert escaped['isError'] is True
        assert '原文证据' not in json.dumps(escaped, ensure_ascii=False)
    assert test_client.get(f'/api/v1/knowledge/enhancements/{pairs[1][1]}', headers=pat_headers).status_code == 200
    assert test_client.get(read_url, headers={'Authorization': f'Bearer {no_read_token}'}).status_code == 403
    assert mcp('knowledge_get_enhancement', {'run_id': run_id},
               {'Authorization': f'Bearer {no_read_token}'})['isError'] is True
    assert test_client.get(read_url, headers=grant_headers, params={'document_ids': pairs[1][0]}).status_code == 404

    async def remove_scope_binding():
        async for db in app.dependency_overrides[get_db]():
            assert await db.scalar(select(func.count()).select_from(ProcessingJob)) == 3
            await db.execute(delete(DocumentScopeKey).where(DocumentScopeKey.document_id == doc_id))
            await db.commit()
    asyncio.run(remove_scope_binding())
    assert test_client.get(read_url, headers=grant_headers).status_code == 404
    assert mcp('knowledge_get_enhancement', {'run_id': run_id})['isError'] is True
    assert test_client.get(read_url, headers=pat_headers).status_code == 200
