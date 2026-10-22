import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta

from sqlalchemy import select

from apps.api.core.db import get_db
from apps.api.main import app
from apps.api.models.webdav import WebDAVEntry, WebDAVScan, WebDAVScanFile
from apps.api.services import webdav_scan_jobs as jobs
from apps.api.services.webdav import RemoteEntry, WebDAVError


def entry(path, directory=False):
    return RemoteEntry(path, directory, "v1", None, 12, "text/markdown")


def factory():
    return asynccontextmanager(app.dependency_overrides[get_db])


def source(client):
    http, _ = client
    row = http.post('/api/webdav', json={
        'name': '后台扫描', 'base_url': 'https://dav.example/', 'root_path': '/docs',
        'username': 'reader', 'password': 'test',
    }).json()
    return http, row, f"/api/webdav/{row['id']}"


def drive(loader):
    return asyncio.run(jobs.process_scan_batch(factory(), list_directory=loader))


def test_enqueue_is_immediate_deduplicated_and_mutations_guarded(client):
    http, row, path = source(client)
    first = http.post(path + '/scan-jobs')
    assert first.status_code == 202
    assert first.json()['status'] == 'queued'
    assert http.post(path + '/scan-jobs').json()['id'] == first.json()['id']
    assert http.get('/api/webdav').json()[0]['scan']['id'] == first.json()['id']
    assert http.post(path + '/sync').status_code == 409
    assert http.post(path + '/scan').status_code == 409
    assert http.patch(path + '/enabled', json={'is_enabled': False}).status_code == 409
    assert http.delete(path).status_code == 409
    assert http.post(path + '/scan-jobs/cancel').json()['status'] == 'cancelled'
    assert http.post(path + '/scan-jobs?resume=true').json()['resumed'] is True


def test_complete_tree_publishes_only_at_end_and_cleans_staging(client):
    http, row, path = source(client)
    http.post(path + '/scan-jobs')
    calls = []
    async def loader(**kw):
        calls.append(kw['root_path'])
        return {
            '/docs': [entry('/docs/sub', True), entry('/docs/a.md')],
            '/docs/sub': [entry('/docs/sub/deep', True)],
            '/docs/sub/deep': [entry('/docs/sub/deep/b.md')],
        }[kw['root_path']]
    assert drive(loader)
    assert http.get(path + '/entries').json() == []
    assert http.get(path + '/scan-jobs').json()['scanned_directories'] == 1
    assert drive(loader)
    assert http.get(path + '/entries').json() == []
    assert drive(loader)
    final = http.get(path + '/scan-jobs').json()
    assert final['status'] == 'completed'
    assert final['result']['missing_check_performed'] is True
    assert final['result']['eligible_files'] == 2
    assert len(http.get(path + '/entries').json()) == 2
    assert len(calls) == 3
    async def check():
        async with factory()() as db:
            assert (await db.scalars(select(WebDAVScanFile))).all() == []
    asyncio.run(check())


def test_failed_directory_retains_successful_siblings_and_resume_skips_missing(client):
    http, row, path = source(client)
    async def seed():
        async with factory()() as db:
            db.add(WebDAVEntry(source_id=row['id'], remote_path='/docs/old.md', state='synced'))
            await db.commit()
    asyncio.run(seed())
    http.post(path + '/scan-jobs')
    failed = True
    calls = []
    in_flight = peak = 0
    async def loader(**kw):
        nonlocal in_flight, peak
        directory = kw['root_path']
        calls.append(directory)
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.001)
        in_flight -= 1
        if directory == '/docs':
            return [entry('/docs/a', True), entry('/docs/b', True), entry('/docs/c', True)]
        if directory == '/docs/b' and failed:
            raise WebDAVError('暂时无权限')
        return [entry(directory + '/file.md')]
    drive(loader)
    drive(loader)
    progress = http.get(path + '/scan-jobs').json()
    assert progress['status'] == 'failed'
    assert progress['scanned_directories'] == 3
    assert progress['pending_directories'] == 1
    assert peak == 3
    assert http.get(path + '/entries').json()[0]['state'] == 'synced'
    failed = False
    assert http.post(path + '/scan-jobs?resume=true').status_code == 202
    drive(loader)
    final = http.get(path + '/scan-jobs').json()
    assert final['status'] == 'completed'
    assert not final['result']['missing_check_performed']
    assert calls.count('/docs/a') == 1 and calls.count('/docs/b') == 2
    old = next(e for e in http.get(path + '/entries').json() if e['remote_path'] == '/docs/old.md')
    assert old['state'] == 'synced'


def test_cancel_fences_late_network_and_changed_config_cannot_resume(client):
    http, row, path = source(client)
    http.post(path + '/scan-jobs')
    async def loader(**_):
        assert http.post(path + '/scan-jobs/cancel').status_code == 200
        return [entry('/docs/a.md')]
    drive(loader)
    assert http.get(path + '/scan-jobs').json()['status'] == 'cancelled'
    assert http.get(path + '/entries').json() == []
    assert http.patch(path, json={
        'name': row['name'], 'base_url': row['base_url'], 'root_path': '/other',
        'username': 'reader', 'password': '',
    }).status_code == 200
    assert http.post(path + '/scan-jobs?resume=true').status_code == 409
    assert http.post(path + '/scan-jobs').status_code == 202


def test_expired_lease_reclaimed_without_missing_confirmation(client):
    http, _, path = source(client)
    http.post(path + '/scan-jobs')
    async def crash():
        async with factory()() as db:
            run = await db.scalar(select(WebDAVScan))
            run.status = 'running'
            run.lease_token = 'crashed'
            run.lease_until = jobs.utcnow() - timedelta(seconds=1)
            await db.commit()
    asyncio.run(crash())
    async def loader(**_):
        return []
    drive(loader)
    final = http.get(path + '/scan-jobs').json()
    assert final['status'] == 'completed' and final['resumed']
    assert not final['result']['missing_check_performed']


def test_limits_and_unexpected_errors_preserve_live_records(client, monkeypatch):
    http, _, path = source(client)
    http.post(path + '/scan-jobs')
    monkeypatch.setattr(jobs, 'MAX_ENTRIES', 1)
    async def loader(**_):
        return [entry('/docs/a.md'), entry('/docs/b.md')]
    drive(loader)
    assert http.get(path + '/scan-jobs').json()['status'] == 'failed'
    assert http.get(path + '/entries').json() == []
    http.post(path + '/scan-jobs')
    async def broken(**_):
        raise RuntimeError('secret-must-not-be-exposed')
    drive(broken)
    assert 'secret-must-not-be-exposed' not in str(http.get(path + '/scan-jobs').json())


def test_background_routes_enforce_workspace_and_auth(client):
    from apps.api.api.auth import require_admin
    http, _, path = source(client)
    created = http.post('/api/workspaces', json={'name': '别的空间', 'slug': 'other'} )
    assert created.status_code == 201
    for method, suffix in [('get', '/scan-jobs'), ('post', '/scan-jobs'), ('post', '/scan-jobs/cancel')]:
        assert getattr(http, method)(path + suffix, headers={'X-Cangzhi-Workspace': 'other'}).status_code == 404
    app.dependency_overrides.pop(require_admin)
    assert http.post(path + '/scan-jobs').status_code == 401


def test_unreadable_credentials_fail_visibly_without_network(client, monkeypatch):
    http, _, path = source(client)
    http.post(path + '/scan-jobs')
    def broken(_):
        raise ValueError('private-key-details')
    monkeypatch.setattr(jobs, 'decrypt_secret', broken)
    async def loader(**_):
        raise AssertionError('must not send a request')
    drive(loader)
    result = http.get(path + '/scan-jobs').json()
    assert result['status'] == 'failed'
    assert '无法解密' in result['error']
    assert 'private-key-details' not in str(result)
