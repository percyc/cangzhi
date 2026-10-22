"""Opt-in against a disposable migrated PostgreSQL database, never production."""
import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apps.api.models.webdav import WebDAVEntry, WebDAVScan, WebDAVSource
from apps.api.models.workspaces import Workspace
from apps.api.security.secrets import encrypt_secret
from apps.api.services.webdav import RemoteEntry
from apps.api.services.webdav_scan_jobs import cancel_scan, process_scan_batch, start_scan


@pytest.mark.asyncio
async def test_postgres_enqueue_claim_and_cancel_fencing():
    url = os.environ.get('TEST_WEBDAV_POSTGRES_URL')
    if not url:
        pytest.skip('requires disposable PostgreSQL')
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workspace_id = None
    try:
        async with sessions() as db:
            workspace = Workspace(slug='scan-' + uuid4().hex, name='synthetic', status='active', settings={})
            db.add(workspace)
            await db.flush()
            workspace_id = workspace.id
            source = WebDAVSource(workspace_id=workspace.id, name='synthetic',
                base_url='https://dav.example/', root_path='/docs', username='test',
                password_cipher=encrypt_secret('test'), include_extensions=['.md'], ignore_patterns=[])
            db.add(source)
            await db.commit()
            source_id = source.id
        async def enqueue():
            async with sessions() as db:
                return await start_scan(db, source_id)
        first, second = await asyncio.gather(enqueue(), enqueue())
        assert first['id'] == second['id']
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def loader(**kw):
            calls.append(kw['root_path'])
            entered.set()
            await release.wait()
            return [RemoteEntry('/docs/file.md', False, '1', None, 1, 'text/markdown')]
        task = asyncio.create_task(process_scan_batch(sessions, list_directory=loader))
        await asyncio.wait_for(entered.wait(), 5)
        assert not await process_scan_batch(sessions, list_directory=loader)
        async with sessions() as db:
            assert (await cancel_scan(db, source_id))['status'] == 'cancelled'
        # Start a new generation before the old request completes.
        restarted = await enqueue()
        assert restarted['id'] != first['id']
        release.set()
        await task
        async with sessions() as db:
            assert (await db.scalars(select(WebDAVEntry).where(WebDAVEntry.source_id == source_id))).all() == []
            run = await db.scalar(select(WebDAVScan).where(WebDAVScan.source_id == source_id))
            assert run.status == 'queued'
        assert await process_scan_batch(sessions, list_directory=loader)
        async with sessions() as db:
            assert len((await db.scalars(select(WebDAVEntry).where(WebDAVEntry.source_id == source_id))).all()) == 1
            run = await db.scalar(select(WebDAVScan).where(WebDAVScan.source_id == source_id))
            assert run.status == 'completed'
        assert len(calls) == 2
    finally:
        if workspace_id:
            async with sessions() as db:
                await db.execute(delete(Workspace).where(Workspace.id == workspace_id))
                await db.commit()
        await engine.dispose()
