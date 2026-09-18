import asyncio
import json

from sqlalchemy import select

from apps.api.core.db import get_db
from apps.api.main import app
from apps.api.models.documents import Document, DocumentVersion, DocumentSourceType
from apps.api.models.enhancement import EnhancementNode, EnhancementRun, EnhancementWindow
from apps.api.models.document_scope_keys import DocumentScopeKey
from apps.api.services import knowledge_enhancement as service
from apps.api.services.workspaces import bind_workspace_context
from test_enhancement_adapters import _owner, _token, _mcp_call


def test_overview_rest_mcp_grant_and_evidence_path(client):
    test_client, _ = client
    _owner(test_client)
    space = test_client.get('/api/workspaces/current').json()['id']
    async def seed():
        async for db in app.dependency_overrides[get_db]():
            bind_workspace_context(db.sync_session, space)
            await db.run_sync(lambda s: service.save_settings(s, {'enabled': True,
                'modules': ['chapter', 'graph', 'overview'], 'call_budget': 8, 'cost_acknowledged': True}))
            doc = Document(title='两个同名主体', source_type=DocumentSourceType.note, workspace_id=space)
            db.add(doc)
            await db.flush()
            version = DocumentVersion(document_id=doc.id, version_number=1, content_hash='test',
                structured_content={'document_type': 'note', 'blocks': [
                    {'type': 'heading', 'text': '甲章', 'level': 1, 'heading_path': []},
                    {'type': 'paragraph', 'text': '甲机构的张某负责采购', 'heading_path': ['甲章']},
                    {'type': 'heading', 'text': '乙章', 'level': 1, 'heading_path': []},
                    {'type': 'paragraph', 'text': '乙机构的张某负责审计', 'heading_path': ['乙章']}]})
            db.add(version)
            await db.flush()
            doc.current_version_id = version.id
            db.add(DocumentScopeKey(document_id=doc.id, workspace_id=space, scope_key='test-overview'))
            await db.flush()
            created = await db.run_sync(lambda s: service.start_run(s, doc.id, cost_acknowledged=True))
            run = await db.get(EnhancementRun, created['id'])
            run.status = 'completed'
            windows = (await db.scalars(select(EnhancementWindow).where(EnhancementWindow.run_id == run.id))).all()
            assert len(windows) == 2
            for window in windows:
                window.status = 'completed'
                window.result = {'summary': {'text': '各自的职责', 'evidence_ids': [1]},
                    'entities': [{'id': 'e1', 'name': '张某', 'kind': 'person', 'aliases': [], 'evidence_ids': [1]}]}
            node = await db.scalar(select(EnhancementNode).where(EnhancementNode.run_id == run.id))
            node.status = 'completed'
            node.result = {'summary': {'text': '两个不同机构的主体', 'support_refs': node.children},
                           'evidence_status': 'model_extracted_unverified'}
            await db.commit()
            return doc.id, run.id, version.id
    doc_id, run_id, version_id = asyncio.run(seed())
    pat = _token(test_client, 'overview', ['knowledge:read'], workspace_id=space)
    search_pat = _token(test_client, 'not-read', ['knowledge:search'], workspace_id=space)
    headers = {'Authorization': f'Bearer {pat}'}
    grant = test_client.post('/api/v1/exploration-grants', headers=headers,
        json={'document_selection': {'scope_keys': ['test-overview']}, 'ttl_seconds': 600}).json()['token']
    path = f'/api/v1/knowledge/enhancements/{run_id}/overview'
    test_client.cookies.clear()
    result = test_client.get(path, headers={'Authorization': f'Bearer {grant}'})
    assert result.status_code == 200
    body = result.json()
    mcp = _mcp_call(client, 'tools/call', {'name': 'knowledge_get_enhancement_overview',
        'arguments': {'run_id': run_id}}, grant).json()['result']
    assert mcp['structuredContent'] == body
    assert body['node']['result']['summary']['support_refs'] == ['w:0', 'w:1']
    candidate = body['node']['entity_candidates']['items'][0]
    assert candidate['identity_status'] == 'unresolved'
    assert {m['window_index'] for m in candidate['mentions']} == {0, 1}
    for child in body['node']['children']:
        evidence = test_client.get(f'/api/v1/knowledge/enhancements/{run_id}', headers=headers,
            params={'view': 'evidence', 'window_index': child['window_index']}).json()
        assert evidence['items'][1]['text'].endswith(('采购', '审计'))
    assert body['read_only'] and body['model_calls'] == 0
    assert 'source_snapshot' not in json.dumps(body) and 'scope_keys' not in json.dumps(body)
    assert test_client.get(path, headers=headers, params={'node_key': 'L9:0'}).status_code == 404
    assert test_client.get(path, headers=headers, params={'node_key': '../bad'}).status_code == 400
    assert test_client.get(path, headers=headers, params={'document_ids': doc_id + 999}).status_code == 404
    assert test_client.get(path, headers={'Authorization': f'Bearer {search_pat}'}).status_code == 403
    outside = test_client.post('/api/v1/exploration-grants', headers=headers,
        json={'document_selection': {'document_ids': [doc_id + 999]}, 'ttl_seconds': 600}).json()['token']
    assert test_client.get(path, headers={'Authorization': f'Bearer {outside}'}).status_code == 404
    assert _mcp_call(client, 'tools/call', {'name': 'knowledge_get_enhancement_overview',
        'arguments': {'run_id': run_id}}, outside).json()['result']['isError']
    async def change():
        async for db in app.dependency_overrides[get_db]():
            version = await db.get(DocumentVersion, version_id)
            version.structured_content = {'document_type': 'note', 'blocks': [{'type': 'paragraph', 'text': '新版'}]}
            await db.commit()
    asyncio.run(change())
    assert test_client.get(path, headers=headers).status_code == 409
