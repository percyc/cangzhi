"""Low-priority collector: one connection, bounded SQL, no recursive disk walks.

Each section has its own freshness/error. PostgreSQL advisory transaction locks
and persisted attempt times prevent duplicate collectors after restart/scale-out.
"""
from datetime import datetime, timedelta, timezone
import shutil
import threading
import time

from sqlalchemy import create_engine, delete, text
from sqlalchemy.orm import Session
from apps.api.models.monitoring import MonitoringHistory, MonitoringSnapshot
from apps.api.services.monitoring import INTERVALS, aware


def collect_knowledge(db):
    queries = {
        "documents": "SELECT count(*) FROM documents WHERE NOT is_deleted",
        "files": "SELECT count(*) FROM documents WHERE NOT is_deleted AND source_type='file'",
        "trash": "SELECT count(*) FROM documents WHERE is_deleted",
        "versions": "SELECT count(*) FROM document_versions",
        "chunks": "SELECT count(*) FROM document_chunks c JOIN documents d ON d.current_version_id=c.document_version_id WHERE NOT d.is_deleted AND c.is_current AND c.role='child'",
        "parent_chunks": "SELECT count(*) FROM document_chunks c JOIN documents d ON d.current_version_id=c.document_version_id WHERE NOT d.is_deleted AND c.is_current AND c.role='parent'",
        "vectors": "SELECT count(*) FROM chunk_embeddings",
        "active_vectors": "SELECT count(*) FROM chunk_embeddings e JOIN embedding_profiles p ON p.id=e.profile_id JOIN document_chunks c ON c.id=e.chunk_id JOIN documents d ON d.current_version_id=c.document_version_id WHERE p.status='active' AND NOT d.is_deleted AND c.is_current AND c.role='child' AND e.content_hash=c.content_hash",
        "datasets": "SELECT count(*) FROM knowledge_datasets k JOIN documents d ON d.current_version_id=k.document_version_id WHERE NOT d.is_deleted AND k.status='ready'",
    }
    return {key: int(db.scalar(text(query))) for key, query in queries.items()}


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


def collect_once(engine, storage_path, *, now=None):
    now = now or datetime.now(timezone.utc)
    deadline = time.monotonic() + 30
    for index, (kind, interval) in enumerate(INTERVALS.items()):
        if time.monotonic() >= deadline:
            break
        with Session(engine) as db, db.begin():
            if engine.dialect.name == "postgresql":
                db.execute(text("SET LOCAL statement_timeout = '2500ms'"))
                db.execute(text("SET LOCAL lock_timeout = '200ms'"))
                db.execute(text("SET LOCAL max_parallel_workers_per_gather = 0"))
                if not db.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": 9340040 + index}):
                    continue
            snapshot = db.get(MonitoringSnapshot, kind)
            if snapshot and (now - aware(snapshot.attempted_at)).total_seconds() < interval:
                continue
            if snapshot is None:
                snapshot = MonitoringSnapshot(kind=kind, attempted_at=now)
                db.add(snapshot)
                db.flush()
            snapshot.attempted_at = now
            try:
                with db.begin_nested():
                    if kind == "queue":
                        data = collect_queue(db)
                    elif kind == "knowledge":
                        data = collect_knowledge(db)
                    else:
                        data = collect_capacity(db, storage_path)
                snapshot.data, snapshot.sampled_at, snapshot.error = data, now, None
            except Exception:
                snapshot.error = "采集超时或暂不可用，保留上次结果；后台稍后重试。"
            if kind == "capacity" and not snapshot.error:
                knowledge = db.get(MonitoringSnapshot, "knowledge")
                hour = now.replace(minute=0, second=0, microsecond=0)
                point = db.get(MonitoringHistory, hour)
                if point is None:
                    point = MonitoringHistory(hour=hour, data={})
                    db.add(point)
                # Never use stale knowledge as a fresh point.
                point.data = {"documents": knowledge.data["documents"] if knowledge and knowledge.data and
                    (now - aware(knowledge.sampled_at)).total_seconds() < 1800 else None,
                    "blob_bytes": snapshot.data["blob_bytes"],
                    "disk_used": snapshot.data["disk"]["used"] if snapshot.data["disk"] else None}
                db.execute(delete(MonitoringHistory).where(MonitoringHistory.hour < now - timedelta(days=31)))


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
                    import logging
                    logging.getLogger(__name__).warning("Operational snapshot collection unavailable")
                stop.wait(60)
        finally:
            engine.dispose()
    thread = threading.Thread(target=run, name="operations-monitor", daemon=True)
    thread.start()
    return stop, thread
