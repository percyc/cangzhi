import asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy import event
from apps.api.core.db import get_db
from apps.api.api.auth import require_admin
from apps.api.main import app
from apps.api.models.monitoring import MonitoringHistory, MonitoringMetrics, MonitoringSnapshot


def _seed(rows):
    async def prepare():
        async for db in app.dependency_overrides[get_db]():
            for row in rows:
                db.add(row)
            await db.commit()
    asyncio.run(prepare())


def test_overview_empty_is_unknown_and_admin_only(client):
    http, _ = client
    response = http.get('/api/system/overview')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.json()['sections']['knowledge']['data'] is None
    assert response.json()['sections']['knowledge']['state'] == 'waiting'
    assert response.json()['sections']['knowledge']['stale'] is True
    assert http.get('/api/system/overview?days=31').status_code == 422
    app.dependency_overrides.pop(require_admin)
    assert http.get('/api/system/overview').status_code == 401


def test_overview_reads_only_bounded_snapshots(client):
    http, _ = client
    queries = []
    now = datetime.now(timezone.utc)
    _seed([MonitoringSnapshot(kind='queue', data={'waiting': 7}, sampled_at=now - timedelta(hours=1),
                              attempted_at=now, error='采集暂不可用'),
            MonitoringHistory(hour=now, data={'documents': 9, 'blob_bytes': 100})])
    async def listen():
        async for db in app.dependency_overrides[get_db]():
            event.listen(db.bind.sync_engine, 'before_cursor_execute',
                         lambda conn, cursor, statement, params, ctx, many: queries.append(statement))
    asyncio.run(listen())
    body = http.get('/api/system/overview').json()
    queue = body['sections']['queue']
    assert queue['data']['waiting'] == 7
    assert queue['state'] == 'degraded'  # legacy snapshot error with retained old value
    assert queue['metrics'][0]['state'] == 'degraded' and queue['metrics'][0]['value'] == 7
    assert queue['stale'] is True
    assert len(body['history']) == 1
    # snapshot select + metric select + history select, all bounded and table-scoped.
    assert len(queries) == 3
    assert all('monitoring_' in q and 'LIMIT' in q for q in queries)


def test_partial_knowledge_with_never_degraded_and_ok(client):
    http, _ = client
    now = datetime.now(timezone.utc)
    _seed([
        MonitoringSnapshot(kind='knowledge', data={'documents': 5, 'active_vectors': 300},
                           sampled_at=now - timedelta(hours=2), attempted_at=now),
        MonitoringMetrics(kind='knowledge', metric='documents', value=5,
                          last_success_at=now - timedelta(seconds=30),
                          last_attempt_at=now - timedelta(seconds=30), consecutive_failures=0),
        MonitoringMetrics(kind='knowledge', metric='active_vectors', value=300,
                          last_success_at=now - timedelta(hours=2), last_attempt_at=now,
                          error='采集超时或锁等待放弃，保留上次结果，按退避稍后重试。', consecutive_failures=3,
                          next_attempt_at=now + timedelta(minutes=5)),
    ])
    body = http.get('/api/system/overview').json()
    kn = body['sections']['knowledge']
    by_name = {m['name']: m for m in kn['metrics']}
    assert kn['state'] == 'partial'
    assert by_name['documents']['state'] == 'ok' and by_name['documents']['value'] == 5
    assert by_name['active_vectors']['state'] == 'degraded'
    assert by_name['active_vectors']['value'] == 300 and by_name['active_vectors']['stale'] is True
    assert by_name['chunks']['state'] == 'never'  # missing row -> never attempted
    assert by_name['active_vectors']['next_attempt_at'] is not None


def test_first_failed_attempted_but_never_succeeded(client):
    http, _ = client
    now = datetime.now(timezone.utc)
    _seed([MonitoringSnapshot(kind='knowledge', data=None, sampled_at=None,
                              attempted_at=now - timedelta(minutes=5), error='采集暂不可用，保留上次结果，按退避稍后重试。')])
    body = http.get('/api/system/overview').json()
    assert body['sections']['knowledge']['state'] == 'first_failed'
    assert body['sections']['knowledge']['metrics'] == []