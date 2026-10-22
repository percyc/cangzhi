import asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy import event
from apps.api.core.db import get_db
from apps.api.api.auth import require_admin
from apps.api.main import app
from apps.api.models.monitoring import MonitoringHistory, MonitoringSnapshot


def test_overview_empty_is_unknown_and_admin_only(client):
    http, _ = client
    response = http.get('/api/system/overview')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.json()['sections']['knowledge']['data'] is None
    assert response.json()['sections']['knowledge']['stale'] is True
    assert http.get('/api/system/overview?days=31').status_code == 422
    app.dependency_overrides.pop(require_admin)
    assert http.get('/api/system/overview').status_code == 401


def test_overview_reads_only_bounded_snapshots(client):
    http, _ = client
    queries = []
    async def prepare():
        async for db in app.dependency_overrides[get_db]():
            now = datetime.now(timezone.utc)
            db.add(MonitoringSnapshot(kind='queue', data={'waiting': 7}, sampled_at=now-timedelta(hours=1), attempted_at=now, error='采集暂不可用'))
            db.add(MonitoringHistory(hour=now, data={'documents': 9, 'blob_bytes': 100}))
            await db.commit()
            event.listen(db.bind.sync_engine, 'before_cursor_execute', lambda conn, cursor, statement, params, ctx, many: queries.append(statement))
    asyncio.run(prepare())
    body = http.get('/api/system/overview').json()
    assert body['sections']['queue']['data']['waiting'] == 7
    assert body['sections']['queue']['stale'] is True
    assert len(body['history']) == 1
    assert len(queries) == 2
    assert all('monitoring_' in q and 'LIMIT' in q for q in queries)
