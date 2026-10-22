"""Low-priority collector: one connection, bounded SQL, no recursive disk walks.

Each metric has its own savepoint and persists its own value / last success /
last attempt / sanitized error / consecutive failures / next attempt time
(ADR-033). A single SQL failure rolls back only that metric and other metrics
continue within the remaining round budget. Healthy metrics keep their normal
cadence; a failing metric retries earlier on bounded, jittered backoff persisted
in ``next_attempt_at``. PostgreSQL advisory transaction locks (keyed by kind,
independent of lock-acquisition success) and the persisted retry times together
prevent restart / multi-instance retry storms.
"""
from datetime import datetime, timedelta, timezone
import logging
import random
import shutil
import threading
import time

import sqlalchemy
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.orm import Session
from apps.api.models.monitoring import MonitoringHistory, MonitoringMetrics, MonitoringSnapshot
from apps.api.services.monitoring import INTERVALS, aware

ACTIVE_VECTOR_COUNT_SQL = """SELECT count(*) FILTER (WHERE e.content_hash=c.content_hash)
    FROM chunk_embeddings e JOIN embedding_profiles p ON p.id=e.profile_id
    JOIN document_chunks c ON c.id=e.chunk_id
    JOIN documents d ON d.current_version_id=c.document_version_id
    WHERE p.status='active' AND NOT d.is_deleted AND c.is_current AND c.role='child'"""

# Bounded per-round and per-SQL budgets in seconds. The per-SQL budget must never
# let the whole round exceed ROUND_BUDGET_S: later SQL gets tightened to remaining.
ROUND_BUDGET_S = 30.0
SQL_BUDGET_S = 2.5
_STOP_SLACK_S = 0.5

# Bounded retry pacing: first failure may retry far sooner than the 15-minute
# healthy cadence, but never faster than BACKOFF_MIN_S, with jitter, and the
# final (post-jitter) value is clamped to a hard cap.
BACKOFF_MIN_S = 60.0
BACKOFF_MAX_S = 3600.0
_JITTER_MIN = 0.8
_JITTER_MAX = 1.2
_EXPONENT_CAP = 8

ERROR_TIMEOUT = "采集超时或锁等待放弃"
ERROR_UNAVAILABLE = "采集暂不可用"
SUMMARY_PARTIAL = "部分指标采集失败，保留旧值；后台按退避稍后重试。"

KNOWLEDGE_QUERIES = {
    "documents": "SELECT count(*) FROM documents WHERE NOT is_deleted",
    "files": "SELECT count(*) FROM documents WHERE NOT is_deleted AND source_type='file'",
    "trash": "SELECT count(*) FROM documents WHERE is_deleted",
    "versions": "SELECT count(*) FROM document_versions",
    "chunks": "SELECT count(*) FROM document_chunks c JOIN documents d ON d.current_version_id=c.document_version_id WHERE NOT d.is_deleted AND c.is_current AND c.role='child'",
    "parent_chunks": "SELECT count(*) FROM document_chunks c JOIN documents d ON d.current_version_id=c.document_version_id WHERE NOT d.is_deleted AND c.is_current AND c.role='parent'",
    "vectors": "SELECT count(*) FROM chunk_embeddings",
    # Hash equality is a validity check, not a join key. Treating the
    # correlated hashes as independent join selectivity can underestimate
    # tens of thousands of matches as one and repeatedly scan documents.
    "active_vectors": ACTIVE_VECTOR_COUNT_SQL,
    "datasets": "SELECT count(*) FROM knowledge_datasets k JOIN documents d ON d.current_version_id=k.document_version_id WHERE NOT d.is_deleted AND k.status='ready'",
}


def collect_knowledge(db):
    """Group dict collector, each metric its own scalar query (raised on error)."""
    return {name: int(db.scalar(text(sql))) for name, sql in KNOWLEDGE_QUERIES.items()}


def collect_queue(db):
    base = " FROM processing_jobs j JOIN documents d ON d.current_version_id=j.document_version_id WHERE NOT d.is_deleted AND j.status IN ('created','retry','processing','failed')"
    states = dict(db.execute(text("SELECT j.status, count(*)" + base + " GROUP BY j.status")).all())
    oldest = db.scalar(text("SELECT min(j.created_at)" + base + " AND j.status IN ('created','retry')"))
    if isinstance(oldest, str):
        oldest = datetime.fromisoformat(oldest)
    return {"waiting": states.get("created", 0) + states.get("retry", 0),
            "running": states.get("processing", 0), "failed": states.get("failed", 0),
            "oldest_waiting_at": aware(oldest).isoformat() if oldest else None}


def collect_capacity(db, storage_path):
    # statvfs only, no file traversal. Failure is explicit, never a false zero.
    try:
        disk = shutil.disk_usage(storage_path)
        volume = {"total": disk.total, "used": disk.used, "free": disk.free}
    except OSError:
        volume = None
    row = db.execute(text("""SELECT count(*) AS blobs, coalesce(sum(file_size),0) AS blob_bytes,
        coalesce(sum(CASE WHEN EXISTS (SELECT 1 FROM document_versions v WHERE v.blob_id=b.id)
        THEN file_size ELSE 0 END),0) AS original_bytes,
        coalesce(sum(CASE WHEN NOT EXISTS (SELECT 1 FROM document_versions v WHERE v.blob_id=b.id)
        AND EXISTS (SELECT 1 FROM document_versions v WHERE v.preview_blob_id=b.id)
        THEN file_size ELSE 0 END),0) AS preview_bytes FROM blobs b""")).mappings().one()
    data = {k: int(v) for k, v in row.items()}
    data["other_blob_bytes"] = data["blob_bytes"] - data["original_bytes"] - data["preview_bytes"]
    data["parquet_bytes"] = int(db.scalar(text("SELECT coalesce(sum(byte_size),0) FROM dataset_artifacts WHERE status='ready'")))
    data["disk"] = volume
    data["database_bytes"] = None
    data["vector_table_bytes"] = None
    if db.bind.dialect.name == "postgresql":
        data["database_bytes"] = int(db.scalar(text("SELECT pg_database_size(current_database())")))
        data["vector_table_bytes"] = int(db.scalar(text("SELECT pg_total_relation_size('chunk_embeddings')")))
    return data


def _blob_metrics(db):
    row = db.execute(text("""SELECT count(*) AS blobs, coalesce(sum(file_size),0) AS blob_bytes,
        coalesce(sum(CASE WHEN EXISTS (SELECT 1 FROM document_versions v WHERE v.blob_id=b.id)
        THEN file_size ELSE 0 END),0) AS original_bytes,
        coalesce(sum(CASE WHEN NOT EXISTS (SELECT 1 FROM document_versions v WHERE v.blob_id=b.id)
        AND EXISTS (SELECT 1 FROM document_versions v WHERE v.preview_blob_id=b.id)
        THEN file_size ELSE 0 END),0) AS preview_bytes FROM blobs b""")).mappings().one()
    blob_bytes = int(row["blob_bytes"])
    return {"blobs": int(row["blobs"]), "blob_bytes": blob_bytes,
            "original_bytes": int(row["original_bytes"]),
            "preview_bytes": int(row["preview_bytes"]),
            "other_blob_bytes": blob_bytes - int(row["original_bytes"]) - int(row["preview_bytes"])}


def _parquet_metrics(db):
    return {"parquet_bytes": int(db.scalar(text("SELECT coalesce(sum(byte_size),0) FROM dataset_artifacts WHERE status='ready'")))}


def _database_size_metrics(db, column):
    # SQLite does not expose PostgreSQL sizing; return None as the truthful N/A value.
    if db.bind.dialect.name != "postgresql":
        return {column: None}
    sql = "SELECT pg_database_size(current_database())" if column == "database_bytes" \
        else "SELECT pg_total_relation_size('chunk_embeddings')"
    return {column: int(db.scalar(text(sql)))}


def _disk_metrics(storage_path):
    # statvfs only, no traversal; OSError surfaces as a metric failure.
    disk = shutil.disk_usage(storage_path)
    return {"disk": {"total": disk.total, "used": disk.used, "free": disk.free}}


def _knowledge_probes():
    return [((name,), _single(name, sql)) for name, sql in KNOWLEDGE_QUERIES.items()]


def _single(name, sql):
    return lambda db: {name: int(db.scalar(text(sql)))}


def _queue_probes():
    return [
        (("waiting", "running", "failed"), lambda db: _queue_states(db)),
        (("oldest_waiting_at",), lambda db: _queue_oldest(db)),
        (("model_pools",), _model_pool_metrics),
        (("waiting_model", "quarantined", "recoveries"), _recovery_metrics),
    ]


def _model_pool_metrics(db):
    rows = db.execute(text("""
        SELECT p.pool_id, p.capacity, p.background_capacity, p.ocr_sublimit,
               p.cooldown_until, count(l.id) AS in_use,
               count(l.id) FILTER (WHERE l.kind='background') AS background_in_use
        FROM model_admission_pools p LEFT JOIN model_admission_leases l
          ON l.pool_id=p.pool_id AND l.lease_until > CURRENT_TIMESTAMP
        GROUP BY p.id ORDER BY p.pool_id LIMIT 32
    """)).mappings().all()
    return {"model_pools": [{**dict(row), "cooldown_until":
        (row.cooldown_until.isoformat() if hasattr(row.cooldown_until, "isoformat")
         else str(row.cooldown_until)) if row.cooldown_until else None} for row in rows]}


def _recovery_metrics(db):
    row = db.execute(text("""
        SELECT count(*) FILTER (WHERE j.status='retry' AND
                   j.error_details->>'deferred'='true') AS waiting_model,
               count(*) FILTER (WHERE j.quarantined_at IS NOT NULL) AS quarantined,
               coalesce(sum(j.recovery_count), 0) AS recoveries
        FROM processing_jobs j JOIN documents d ON d.current_version_id=j.document_version_id
        WHERE NOT d.is_deleted
    """)).mappings().one()
    return dict(row)


def _queue_states(db):
    base = " FROM processing_jobs j JOIN documents d ON d.current_version_id=j.document_version_id WHERE NOT d.is_deleted AND j.status IN ('created','retry','processing','failed')"
    states = dict(db.execute(text("SELECT j.status, count(*)" + base + " GROUP BY j.status")).all())
    return {"waiting": states.get("created", 0) + states.get("retry", 0),
            "running": states.get("processing", 0), "failed": states.get("failed", 0)}


def _queue_oldest(db):
    base = " FROM processing_jobs j JOIN documents d ON d.current_version_id=j.document_version_id WHERE NOT d.is_deleted AND j.status IN ('created','retry','processing','failed')"
    oldest = db.scalar(text("SELECT min(j.created_at)" + base + " AND j.status IN ('created','retry')"))
    if isinstance(oldest, str):
        oldest = datetime.fromisoformat(oldest)
    return {"oldest_waiting_at": aware(oldest).isoformat() if oldest else None}


def _capacity_probes(storage_path):
    return [
        (("blobs", "blob_bytes", "original_bytes", "preview_bytes", "other_blob_bytes"), _blob_metrics),
        (("parquet_bytes",), _parquet_metrics),
        (("database_bytes",), lambda db: _database_size_metrics(db, "database_bytes")),
        (("vector_table_bytes",), lambda db: _database_size_metrics(db, "vector_table_bytes")),
        (("disk",), lambda db: _disk_metrics(storage_path)),
    ]


def _probes_for(kind, storage_path):
    if kind == "knowledge":
        return _knowledge_probes()
    if kind == "queue":
        return _queue_probes()
    return _capacity_probes(storage_path)


def _sanitize_error(exc):
    if isinstance(exc, sqlalchemy.exc.OperationalError):
        return ERROR_TIMEOUT
    return ERROR_UNAVAILABLE


def _backoff_seconds(consecutive):
    exponent = min(consecutive, _EXPONENT_CAP) - 1
    base = BACKOFF_MIN_S * (2 ** exponent)
    jittered = base * random.uniform(_JITTER_MIN, _JITTER_MAX)
    return max(BACKOFF_MIN_S, min(BACKOFF_MAX_S, jittered))


def _seed_from_snapshot(db, snapshot):
    """Seed pre-migration snapshot values into metric rows before first collection.

    Without this, the first metric SQL failure would look like a first-ever failure
    and drop the previously good snapshot value. Legacy successful values keep their
    original timestamps and are not re-stamped as fresh.
    """
    if snapshot is None or not isinstance(snapshot.data, dict) or not snapshot.data:
        return
    for name, value in snapshot.data.items():
        if db.get(MonitoringMetrics, (snapshot.kind, name)) is not None:
            continue
        db.add(MonitoringMetrics(kind=snapshot.kind, metric=name, value=value,
                                 last_success_at=snapshot.sampled_at,
                                 last_attempt_at=snapshot.attempted_at,
                                 consecutive_failures=0,
                                 error=ERROR_UNAVAILABLE if snapshot.error else None,
                                 next_attempt_at=None))


def _metric_due(db, kind, name, interval, now):
    row = db.get(MonitoringMetrics, (kind, name))
    if row is None or row.last_attempt_at is None:
        return True  # never attempted: collect on first opportunity
    last = aware(row.last_attempt_at)
    if row.error:  # failing: paced bounded backoff retry
        next_at = aware(row.next_attempt_at) if row.next_attempt_at else None
        return next_at is None or now >= next_at
    return (now - last).total_seconds() >= interval


def _record_success(db, kind, name, value, now):
    row = db.get(MonitoringMetrics, (kind, name))
    if row is None:
        row = MonitoringMetrics(kind=kind, metric=name)
    row.value = value
    row.last_success_at = now
    row.last_attempt_at = now
    row.error = None
    row.consecutive_failures = 0
    row.next_attempt_at = None
    db.add(row)


def _record_failure(db, kind, name, err, now):
    row = db.get(MonitoringMetrics, (kind, name))
    if row is None:
        row = MonitoringMetrics(kind=kind, metric=name)
    # Retain old value and old last success; never attempt -> last_attempt now set.
    row.last_attempt_at = now
    row.error = err
    row.consecutive_failures = (row.consecutive_failures or 0) + 1
    row.next_attempt_at = now + timedelta(seconds=_backoff_seconds(row.consecutive_failures))
    db.add(row)


def _setup_session_timeouts(db, deadline):
    """Bound SQL/lock cost for metadata reads too, before any metric query."""
    if db.bind.dialect.name != "postgresql":
        return
    remaining = max(0.1, deadline - time.monotonic())
    db.execute(text("SELECT set_config('statement_timeout', :v, true)"),
               {"v": str(int(min(SQL_BUDGET_S, remaining) * 1000))})
    db.execute(text("SELECT set_config('lock_timeout', :v, true)"), {"v": "200"})
    db.execute(text("SELECT set_config('max_parallel_workers_per_gather', :v, true)"), {"v": "0"})


def _tighten_timeout(db, seconds):
    # set_config(..., is_local=true) avoids the bind-variable limitation of
    # `SET LOCAL statement_timeout = :ms` over the PostgreSQL DBAPI.
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT set_config('statement_timeout', :v, true)"),
                   {"v": str(int(max(100, seconds * 1000)))})
        db.execute(text("SELECT set_config('lock_timeout', :v, true)"), {"v": "200"})


def _bookkeeping_timeout(db, deadline):
    """Bound history/metadata bookkeeping SQL to the remaining round budget."""
    remaining = deadline - time.monotonic() - _STOP_SLACK_S
    _tighten_timeout(db, min(SQL_BUDGET_S, max(0.25, remaining)))


def _collect_kind(db, kind, interval, now, deadline, storage_path, snapshot):
    _seed_from_snapshot(db, snapshot)
    logger = logging.getLogger(__name__)
    attempted = False
    for metric_names, probe in _probes_for(kind, storage_path):
        if not any(_metric_due(db, kind, name, interval, now) for name in metric_names):
            continue
        if time.monotonic() >= deadline - _STOP_SLACK_S:
            break  # no budget left: remaining metrics are not-attempted, not failures
        remaining = deadline - time.monotonic() - _STOP_SLACK_S
        _tighten_timeout(db, min(SQL_BUDGET_S, max(0.25, remaining)))
        started = time.monotonic()
        try:
            with db.begin_nested():
                result = probe(db)
            for name, value in result.items():
                _record_success(db, kind, name, value, now)
        except Exception as exc:  # noqa: BLE001 - one probe failing must not hide the rest
            err = _sanitize_error(exc)
            for name in metric_names:
                _record_failure(db, kind, name, err, now)
            # Safe telemetry only: metric identifier, error category, duration.
            logger.warning("metric collection failed kind=%s metrics=%s error=%s duration_ms=%d",
                           kind, ",".join(metric_names), err, int((time.monotonic() - started) * 1000))
        attempted = True
    return attempted


def _refresh_snapshot(db, snapshot):
    rows = db.execute(select(MonitoringMetrics).where(MonitoringMetrics.kind == snapshot.kind)).scalars().all()
    if not rows:
        snapshot.data = None
        return
    snapshot.data = {row.metric: row.value for row in rows}
    successes = [aware(row.last_success_at) for row in rows if row.last_success_at]
    if successes:
        snapshot.sampled_at = max(successes)
    snapshot.error = SUMMARY_PARTIAL if any(row.error for row in rows) else None


def _same_hour(ts, hour):
    return aware(ts).replace(minute=0, second=0, microsecond=0) == hour


def _write_history(db, now):
    """Write a trend point at the hour the measurement actually succeeded.

    Never books a fresh timestamp for an old measurement: the point hour is derived
    from the successful blob sample's own ``last_success_at``, and each other field
    is only populated when it was measured in that same hour window (otherwise it
    keeps its previous value or stays NULL).
    """
    blob = db.get(MonitoringMetrics, ("capacity", "blob_bytes"))
    if blob is None or blob.last_success_at is None:
        return
    blob_hour = aware(blob.last_success_at).replace(minute=0, second=0, microsecond=0)
    point = db.get(MonitoringHistory, blob_hour)
    if point is None:
        point = MonitoringHistory(hour=blob_hour, data={})
        db.add(point)
    doc = db.get(MonitoringMetrics, ("knowledge", "documents"))
    if doc and doc.last_success_at and _same_hour(doc.last_success_at, blob_hour) and doc.value is not None:
        documents = int(doc.value)
    else:
        documents = None
    disk = db.get(MonitoringMetrics, ("capacity", "disk"))
    if disk and disk.last_success_at and _same_hour(disk.last_success_at, blob_hour) \
            and isinstance(disk.value, dict) and disk.value.get("used") is not None:
        disk_used = int(disk.value["used"])
    else:
        disk_used = None
    # Assign a fresh dict: SQLAlchemy's plain JSON column does not detect in-place
    # mutation of an already-persisted point across sessions.
    point.data = {
        "documents": documents if documents is not None else point.data.get("documents"),
        "disk_used": disk_used if disk_used is not None else point.data.get("disk_used"),
        "blob_bytes": int(blob.value),
    }
    db.execute(delete(MonitoringHistory).where(MonitoringHistory.hour < now - timedelta(days=31))
               .execution_options(synchronize_session=False))


def collect_once(engine, storage_path, *, now=None):
    now = now or datetime.now(timezone.utc)
    deadline = time.monotonic() + ROUND_BUDGET_S
    for index, (kind, interval) in enumerate(INTERVALS.items()):
        if time.monotonic() >= deadline:
            break
        with Session(engine) as db, db.begin():
            # Bound SQL/lock cost before metadata reads as well.
            _setup_session_timeouts(db, deadline)
            if engine.dialect.name == "postgresql":
                if not db.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": 9340040 + index}):
                    continue
            snapshot = db.get(MonitoringSnapshot, kind)
            created = snapshot is None
            if snapshot is None:
                snapshot = MonitoringSnapshot(kind=kind, attempted_at=now)
            attempted = _collect_kind(db, kind, interval, now, deadline, storage_path, snapshot)
            if created and not attempted:
                continue  # No budget/probe is not a first failed attempt.
            if attempted:
                snapshot.attempted_at = now
            db.add(snapshot)
            _bookkeeping_timeout(db, deadline)
            _refresh_snapshot(db, snapshot)
            # History is written under the capacity advisory lock and round budget.
            if kind == "capacity" and time.monotonic() < deadline - _STOP_SLACK_S:
                _bookkeeping_timeout(db, deadline)
                _write_history(db, now)


def start_monitor(database_url, storage_path):
    stop = threading.Event()
    def run():
        engine = create_engine(database_url, pool_size=1, max_overflow=0, pool_pre_ping=True,
                               connect_args={"connect_timeout": 3})
        try:
            while not stop.is_set():
                try:
                    collect_once(engine, storage_path)
                except Exception:
                    # Fail closed: no raw SQL/credentials/content in log messages.
                    logging.getLogger(__name__).warning("Operational snapshot collection unavailable")
                stop.wait(60)
        finally:
            engine.dispose()
    thread = threading.Thread(target=run, name="operations-monitor", daemon=True)
    thread.start()
    return stop, thread
