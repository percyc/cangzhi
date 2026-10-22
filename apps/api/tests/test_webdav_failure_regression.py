"""A failed middle file must not abort the following file or lose prior saves."""
from apps.api.services.webdav import RemoteEntry, WebDAVError
import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session
from apps.api.models.documents import Document


@pytest.mark.parametrize('failure', ['download', 'persistence'])
def test_legacy_sync_continues_after_middle_failure(client, monkeypatch, failure):
    http, _ = client
    source = http.post('/api/webdav', json={
        'name': '合成来源', 'base_url': 'https://dav.example/',
        'root_path': '/docs', 'username': 'test', 'password': 'test',
    }).json()

    async def scan(**kwargs):
        return ([RemoteEntry('/docs/' + name, False, 'v1', None, 10, 'text/markdown')
                 for name in ('a.md', 'b.md', 'c.md')], 1, '/docs')

    async def download(**kwargs):
        if failure == 'download' and kwargs['remote_path'].endswith('/b.md'):
            raise WebDAVError('WebDAV 文件内容为空')
        return ('# ' + kwargs['remote_path']).encode(), 'text/markdown'

    monkeypatch.setattr('apps.api.api.webdav.scan_tree', scan)
    monkeypatch.setattr('apps.api.api.webdav.download_file', download)
    assert http.post(f"/api/webdav/{source['id']}/scan").status_code == 200
    def fail_save(session, *_args):
        if failure == 'persistence' and any(isinstance(row, Document) and row.title == 'b'
                                            for row in session.new):
            raise RuntimeError('synthetic persistence failure')
    event.listen(Session, 'before_flush', fail_save)
    try:
        response = http.post(f"/api/webdav/{source['id']}/sync")
    finally:
        event.remove(Session, 'before_flush', fail_save)
    assert response.status_code == 200
    assert response.json()['imported'] == 2
    assert response.json()['failed'] == 1
    entries = http.get(f"/api/webdav/{source['id']}/entries").json()
    assert [item['state'] for item in entries] == ['synced', 'failed', 'synced']
    assert http.get('/api/webdav').json()[0]['sync_status'] == 'failed'
