"""Real source navigation through REST/MCP, without enhancement or model calls."""
import asyncio
from urllib.parse import parse_qs, urlsplit

import pytest

from apps.api.core.db import get_db
from apps.api.main import app
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.document_scope_keys import DocumentScopeKey
from apps.api.services.workspaces import bind_workspace_context
from apps.api.services.knowledge_read import KnowledgeReadError
from apps.api.api.v1 import _source_navigation_error
from apps.api.api.mcp import DocumentMapArguments, DocumentBlockArguments
from pydantic import ValidationError
from apps.cli.main import build_parser, run, CLIError
from test_cli import StubClient
from test_enhancement_adapters import _owner, _token, _mcp_call


def test_source_navigation_rest_mcp_scope_and_version(client):
    web, _ = client
    _owner(web)
    space = web.get('/api/workspaces/current').json()['id']

    async def seed():
        async for db in app.dependency_overrides[get_db]():
            bind_workspace_context(db.sync_session, space)
            doc = Document(title='结构导航', source_type=DocumentSourceType.note, workspace_id=space)
            db.add(doc)
            await db.flush()
            version = DocumentVersion(document_id=doc.id, version_number=1, content_hash='synthetic',
                structured_content={'document_type': 'docx', 'blocks': [
                    {'type': 'heading', 'text': '第一章', 'heading_path': []},
                    {'type': 'table', 'text': '|名称|数量|\n|甲|2|', 'heading_path': ['第一章']},
                    {'type': 'paragraph', 'text': '重复内容'},
                    {'type': 'paragraph', 'text': '重复内容'}]})
            db.add(version)
            await db.flush()
            doc.current_version_id = version.id
            db.add(DocumentScopeKey(document_id=doc.id, workspace_id=space, scope_key='synthetic-nav'))
            await db.commit()
            return doc.id, version.id

    doc_id, version_id = asyncio.run(seed())
    pat = _token(web, 'navigation', ['knowledge:read'], workspace_id=space)
    search_only = _token(web, 'not-read', ['knowledge:search'], workspace_id=space)
    headers = {'Authorization': f'Bearer {pat}'}
    grant = web.post('/api/v1/exploration-grants', headers=headers,
        json={'document_selection': {'scope_keys': ['synthetic-nav']}, 'ttl_seconds': 600}).json()['token']
    outside = web.post('/api/v1/exploration-grants', headers=headers,
        json={'document_selection': {'document_ids': [doc_id + 999]}, 'ttl_seconds': 600}).json()['token']
    web.cookies.clear()
    path = f'/api/v1/knowledge/documents/{doc_id}'
    outline = web.get(path + '/map', headers=headers)
    assert outline.status_code == 200
    assert outline.json()['items'][0]['title'] == '第一章'
    mapped = web.get(path + '/map', headers={'Authorization': f'Bearer {grant}'},
        params={'view': 'blocks', 'limit': 2}).json()
    assert mapped['total'] == 4 and mapped['next_offset'] == 2
    assert mapped['items'][0]['type'] == 'heading'
    assert mapped['read_only'] and mapped['model_calls'] == 0
    mcp = _mcp_call(client, 'tools/call', {'name': 'knowledge_get_document_map',
        'arguments': {'document_id': doc_id, 'view': 'blocks', 'limit': 2}}, grant).json()['result']
    assert mcp['structuredContent'] == mapped
    repeated = web.get(path + '/map', headers=headers,
        params={'view': 'blocks', 'offset': 2, 'limit': 2}).json()
    assert repeated['next_offset'] is None
    assert repeated['items'][0]['id'] != repeated['items'][1]['id']
    for item in repeated['items']:
        assert web.get(path + '/block', headers=headers,
            params={'block_id': item['id']}).json()['block']['text'] == '重复内容'
    block_id = mapped['items'][1]['id']
    params = {'block_id': block_id, 'max_chars': 5}
    block = web.get(path + '/block', headers=headers, params=params).json()
    assert block['block']['text'] == '|名称|数'
    assert block['block']['partial'] and block['block']['next_offset'] == 5
    assert _mcp_call(client, 'tools/call', {'name': 'knowledge_get_document_block',
        'arguments': {'document_id': doc_id, **params}}, grant).json()['result']['structuredContent'] == block
    for suffix, query, tool, args in [('/map', {}, 'knowledge_get_document_map', {}),
            ('/block', params, 'knowledge_get_document_block', params)]:
        assert web.get(path + suffix, headers={'Authorization': f'Bearer {outside}'}, params=query).status_code == 404
        assert web.get(path + suffix, headers={'Authorization': f'Bearer {search_only}'}, params=query).status_code == 403
        assert web.get(path + suffix, headers=headers,
            params={**query, 'document_ids': doc_id + 999}).status_code == 404
        denied = _mcp_call(client, 'tools/call', {'name': tool,
            'arguments': {'document_id': doc_id, **args}}, outside).json()['result']
        assert denied['isError'] and denied['structuredContent']['error']['code'] == 'document_not_found'
    assert web.get(path + '/map', headers=headers, params={'limit': 101}).status_code == 422
    assert web.get(path + '/block', headers=headers, params={'block_id': 'stale'}).status_code == 409

    async def change():
        async for db in app.dependency_overrides[get_db]():
            version = await db.get(DocumentVersion, version_id)
            version.structured_content = {'document_type': 'note', 'blocks': [{'type': 'paragraph', 'text': '新版'}]}
            await db.commit()
    asyncio.run(change())
    assert web.get(path + '/block', headers=headers, params=params).status_code == 409
    stale = _mcp_call(client, 'tools/call', {'name': 'knowledge_get_document_block',
        'arguments': {'document_id': doc_id, **params}}, pat).json()['result']
    assert stale['structuredContent']['error']['code'] == 'source_changed'


@pytest.mark.parametrize('command,extras,suffix,expected', [
    ('document-map', ['--view', 'blocks', '--limit', '2'], 'map', {'view': ['blocks'], 'limit': ['2']}),
    ('document-block', ['--block-id', 'v1:abc:b0', '--max-chars', '30'], 'block',
     {'block_id': ['v1:abc:b0'], 'max_chars': ['30']}),
])
def test_source_cli(command, extras, suffix, expected):
    args = build_parser().parse_args([command, '17', '--offset', '3', '--document-ids', '17,18', *extras])
    client = StubClient()
    run(args, client)
    method, path, payload = client.calls[0]
    assert method == 'GET' and payload is None
    assert urlsplit(path).path == f'/api/v1/knowledge/documents/17/{suffix}'
    assert parse_qs(urlsplit(path).query) == {**expected, 'offset': ['3'], 'document_ids': ['17', '18']}


@pytest.mark.parametrize('args', [
    ['document-map', '1', '--limit', '101'], ['document-map', '1', '--document-ids', ''],
    ['document-block', '1', '--block-id', 'x', '--max-chars', '12001'],
    ['document-block', '1', '--block-id', 'x', '--offset', '-1'],
])
def test_source_cli_rejects_invalid_without_request(args):
    client = StubClient()
    with pytest.raises(CLIError):
        run(build_parser().parse_args(args), client)
    assert not client.calls


@pytest.mark.parametrize('code,status', [('structure_unavailable', 409), ('source_changed', 409),
    ('structure_too_large', 413), ('document_not_found', 404), ('invalid_arguments', 400)])
def test_source_error_status(code, status):
    assert _source_navigation_error(KnowledgeReadError(code, 'safe message')).status_code == status


@pytest.mark.parametrize('model,payload', [
    (DocumentMapArguments, {'document_id': True}),
    (DocumentMapArguments, {'document_id': 1, 'limit': '20'}),
    (DocumentMapArguments, {'document_id': 1, 'view': 'all'}),
    (DocumentBlockArguments, {'document_id': 1, 'block_id': ''}),
    (DocumentBlockArguments, {'document_id': 1, 'block_id': 'x', 'max_chars': 12001}),
    (DocumentBlockArguments, {'document_id': 1, 'block_id': 'x', 'offset': False}),
])
def test_mcp_source_argument_bounds(model, payload):
    with pytest.raises(ValidationError):
        model.model_validate(payload)
