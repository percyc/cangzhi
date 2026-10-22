from apps.api.services.webdav import RemoteEntry


def inventory(client, monkeypatch, names):
    http, _ = client
    source = http.post('/api/webdav', json={
        'name': '合成文件来源', 'base_url': 'https://dav.example/',
        'root_path': '/docs', 'username': 'test', 'password': 'test',
    }).json()

    async def scan_tree(**kwargs):
        return ([RemoteEntry('/docs/' + name, False, 'v1', None, 20, 'text/markdown')
                 for name in names], 3, '/docs')

    monkeypatch.setattr('apps.api.api.webdav.scan_tree', scan_tree)
    assert http.post(f"/api/webdav/{source['id']}/scan").status_code == 200
    return http, f"/api/webdav/{source['id']}/browse"


def test_folders_files_paged_together_and_scoped_counts(client, monkeypatch):
    http, url = inventory(client, monkeypatch,
                          ['a.md', '部门/一.md', '部门/下级/二.md', '另一部门/三.md'])
    first = http.get(url, params={'page_size': 2}).json()
    assert first['total'] == 3
    assert first['root'] == first['directory'] == '/docs'
    assert first['parent'] is None
    assert len(first['items']) == 2
    assert all(row['kind'] == 'folder' for row in first['items'])
    assert first['pending_count'] == 4
    assert http.get(url, params={'page': 2, 'page_size': 2}).json()['items'][0]['name'] == 'a.md'
    folder = http.get(url, params={'directory': '/docs/部门'}).json()
    assert folder['parent'] == '/docs' and folder['pending_count'] == 2
    assert [row['kind'] for row in folder['items']] == ['folder', 'file']
    assert folder['items'][0]['file_count'] == 1
    nested = http.get(url, params={'directory': '/docs/部门', 'recursive': True}).json()
    assert nested['total'] == 2
    assert all(row['kind'] == 'file' for row in nested['items'])


def test_literal_search_and_path_not_double_decoded(client, monkeypatch):
    http, url = inventory(client, monkeypatch,
                          ['a_b/100%.md', 'acb/100x.md', '%2F/保留.md'])
    literal = http.get(url, params={'q': '100%'}).json()
    assert literal['total'] == 1 and literal['items'][0]['name'] == '100%.md'
    under = http.get(url, params={'directory': '/docs/a_b'}).json()
    assert under['total'] == 1 and under['items'][0]['remote_path'] == '/docs/a_b/100%.md'
    encoded = http.get(url, params={'directory': '/docs/%2F'}).json()
    assert encoded['items'][0]['remote_path'] == '/docs/%2F/保留.md'
    assert http.get(url, params={'state': 'failed'}).json()['total'] == 0


def test_bounds_root_and_empty_directory(client, monkeypatch):
    http, url = inventory(client, monkeypatch, ['a.md'])
    for directory in ['/docs2', '/outside', '/docs/../outside', 'relative']:
        assert http.get(url, params={'directory': directory}).status_code == 422
    assert http.get(url, params={'page_size': 101}).status_code == 422
    assert http.get(url, params={'page': 0}).status_code == 422
    assert http.get(url, params={'q': 'x' * 201}).status_code == 422
    empty = http.get(url, params={'directory': '/docs/empty'}).json()
    assert empty['total'] == 0 and empty['items'] == []
    assert http.get('/api/webdav/999999/browse').status_code == 404


def test_browser_auth_and_workspace(client, monkeypatch):
    http, url = inventory(client, monkeypatch, ['a.md'])
    created = http.post('/api/workspaces', json={'name': '隔离空间', 'slug': 'isolated'})
    assert created.status_code in (200, 201)
    assert http.get(url, headers={'X-Cangzhi-Workspace': 'isolated'}).status_code == 404
    from apps.api.main import app
    from apps.api.api.auth import require_admin
    app.dependency_overrides.pop(require_admin)
    assert http.get(url).status_code == 401
