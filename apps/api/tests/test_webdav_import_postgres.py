"""Real row-lock and post-commit scan race on a disposable PostgreSQL only."""
import asyncio
import os

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from apps.api.api import webdav
from apps.api.models.webdav import WebDAVSource, WebDAVEntry
from apps.api.models.documents import Document
from apps.api.models.blobs import Blob
from apps.api.storage.local import LocalBlobStorage


def test_import_lock_and_scan_status_after_commit(tmp_path, monkeypatch):
    url = os.getenv('CANGZHI_WEBDAV_TEST_DATABASE_URL')
    if not url:
        pytest.skip('explicit isolated database required')
    assert url.endswith('/cangzhi_test')
    monkeypatch.setattr(webdav, 'decrypt_secret', lambda _: 'synthetic')

    async def run():
        engine = create_async_engine(url.replace('postgresql://', 'postgresql+asyncpg://', 1))
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        entered, release = asyncio.Event(), asyncio.Event()
        ids = []
        source_id = None
        try:
            async with sessions() as db:
                source = WebDAVSource(name='合成锁测试', base_url='https://dav.example/',
                    root_path='/docs', username='synthetic', password_cipher='',
                    include_extensions=['.md'], ignore_patterns=[])
                db.add(source)
                await db.flush()
                source_id = source.id
                for name in ['a.md', 'b.md']:
                    entry = WebDAVEntry(source_id=source.id, remote_path='/docs/' + name,
                                        state='discovered')
                    db.add(entry)
                    await db.flush()
                    ids.append(entry.id)
                await db.commit()

            async def download(**kwargs):
                entered.set()
                await release.wait()
                return ('# ' + kwargs['remote_path']).encode(), 'text/markdown'
            monkeypatch.setattr(webdav, 'download_file', download)
            storage = LocalBlobStorage(tmp_path)

            async def call_first():
                async with sessions() as db:
                    return await webdav.sync_batch(source_id, webdav.SyncBatchRequest(entry_ids=[ids[0]]),
                                                   db=db, storage=storage)

            first = asyncio.create_task(call_first())
            await asyncio.wait_for(entered.wait(), 5)
            try:
                async with sessions() as db:
                    with pytest.raises(HTTPException) as error:
                        await webdav.sync_batch(source_id, webdav.SyncBatchRequest(entry_ids=[ids[0]]),
                                                db=db, storage=storage)
                    assert error.value.status_code == 409
            finally:
                release.set()
            assert (await first)['imported'] == 1

            original = webdav.run_sync_batch
            async def race(*args, **kwargs):
                result = await original(*args, **kwargs)
                # Another operation begins after the import commits/releases
                # the source lock but before its HTTP handler finalizes status.
                async with sessions() as other:
                    await other.execute(update(WebDAVSource).where(WebDAVSource.id == source_id)
                                        .values(sync_status='scanning'))
                    await other.commit()
                return result
            monkeypatch.setattr(webdav, 'run_sync_batch', race)
            async with sessions() as db:
                result = await webdav.sync_batch(source_id, webdav.SyncBatchRequest(entry_ids=[ids[1]]),
                                                db=db, storage=storage)
                assert result['imported'] == 1
            async with sessions() as db:
                assert (await db.get(WebDAVSource, source_id)).sync_status == 'scanning'
        finally:
            # Only synthetic records created above in the explicit test DB.
            if source_id:
                from sqlalchemy import select
                async with sessions() as db:
                    doc_ids = list((await db.scalars(select(WebDAVEntry.document_id)
                        .where(WebDAVEntry.source_id == source_id))).all())
                    await db.execute(delete(WebDAVSource).where(WebDAVSource.id == source_id))
                    await db.execute(delete(Document).where(Document.id.in_([x for x in doc_ids if x])))
                    # Blob rows are deliberately retained in the disposable DB;
                    # cleanup of production facts is never part of this test.
                    await db.commit()
            await engine.dispose()
    asyncio.run(run())
