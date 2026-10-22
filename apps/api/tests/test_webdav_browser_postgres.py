"""Explicit disposable PostgreSQL only; real SQL folder grouping dialect."""
import asyncio
import os

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from apps.api.models.webdav import WebDAVEntry, WebDAVSource
from apps.api.services.webdav_browser import browse_entries


def test_postgres_folders_pagination_and_literal_paths():
    url = os.getenv('CANGZHI_WEBDAV_TEST_DATABASE_URL')
    if not url:
        pytest.skip('explicit isolated database required')
    assert url.endswith('/cangzhi_test')

    async def run():
        engine = create_async_engine(url.replace('postgresql://', 'postgresql+asyncpg://', 1))
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessions() as db:
                source = WebDAVSource(name='合成目录', base_url='https://dav.example/',
                    root_path='/docs', username='synthetic', password_cipher='',
                    include_extensions=['.md'], ignore_patterns=[])
                db.add(source)
                await db.flush()
                db.add_all([WebDAVEntry(source_id=source.id, remote_path=f'/docs/folder-{i//50}/{i}.md',
                                       state='discovered') for i in range(5000)])
                db.add(WebDAVEntry(source_id=source.id, remote_path='/docs/a_b/100%.md', state='failed'))
                await db.flush()
                root = await browse_entries(db, source, page_size=25)
                assert root['total'] == 101 and len(root['items']) == 25
                assert root['pending_count'] == 5000 and root['failed_count'] == 1
                assert all(row['kind'] == 'folder' for row in root['items'])
                page = await browse_entries(db, source, directory='/docs/folder-0', page=2, page_size=25)
                assert page['total'] == 50 and len(page['items']) == 25
                literal = await browse_entries(db, source, directory='/docs/a_b', q='100%')
                assert literal['total'] == 1 and literal['items'][0]['name'] == '100%.md'
                await db.rollback()
        finally:
            await engine.dispose()
    asyncio.run(run())
