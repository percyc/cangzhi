"""Read snapshots only: page refresh never performs live counts or filesystem work.

Backward-compatible surface: ``sections[kind].data / sampled_at / attempted_at /
error / age_seconds / stale / interval_seconds`` keep their meaning, while per-metric
``metrics`` and a kind-level ``state`` describe never-attempted / first-failed /
partial / previously-succeeded-then-failed clearly. Pre-migration group snapshots
(no metric rows) are mapped read-only to the old state, reflecting ``snapshot.error``
and attempted-but-no-success (ADR-033).
"""
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from ..models.monitoring import MonitoringHistory, MonitoringMetrics, MonitoringSnapshot

INTERVALS = {"queue": 60, "knowledge": 900, "capacity": 900}

# Canonical metric names so the page can list never-attempted metrics truthfully.
METRIC_NAMES = {
    "knowledge": ["documents", "files", "trash", "versions", "chunks",
                  "parent_chunks", "vectors", "active_vectors", "datasets"],
    "queue": ["waiting", "running", "failed", "oldest_waiting_at", "model_pools",
              "waiting_model", "quarantined", "recoveries"],
    "capacity": ["blobs", "blob_bytes", "original_bytes", "preview_bytes",
                 "other_blob_bytes", "parquet_bytes", "database_bytes",
                 "vector_table_bytes", "disk"],
}

# At most 9 + 8 + 9 metrics across the three global kinds.
_METRIC_READ_LIMIT = 100


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _metric_view(now, interval, name, row):
    success_age = attempt_age = None
    if row and row.last_success_at:
        success_age = max(0, int((now - aware(row.last_success_at)).total_seconds()))
    if row and row.last_attempt_at:
        attempt_age = max(0, int((now - aware(row.last_attempt_at)).total_seconds()))

    if row is None or row.last_attempt_at is None:
        return {
            "name": name, "value": None, "last_success_at": None,
            "last_attempt_at": None, "error": None, "consecutive_failures": 0,
            "next_attempt_at": None, "state": "never",
            "success_age_seconds": success_age, "attempt_age_seconds": attempt_age,
            "stale": False,
        }
    state = "ok" if not row.error else ("degraded" if row.last_success_at is not None else "first_failed")
    stale = attempt_age is not None and attempt_age > interval * 3
    if not stale and state in ("ok", "degraded"):
        stale = success_age is None or success_age > interval * 3
    return {
        "name": name,
        "value": row.value,
        "last_success_at": row.last_success_at,
        "last_attempt_at": row.last_attempt_at,
        "error": row.error,
        "consecutive_failures": row.consecutive_failures,
        "next_attempt_at": row.next_attempt_at,
        "state": state,
        "success_age_seconds": success_age,
        "attempt_age_seconds": attempt_age,
        "stale": stale,
    }


def _kind_state(metrics, snapshot):
    if metrics:
        ok = any(m["state"] == "ok" for m in metrics)
        degraded = any(m["state"] == "degraded" for m in metrics)
        first_failed = any(m["state"] == "first_failed" for m in metrics)
        never = any(m["state"] == "never" for m in metrics)
        if ok and not degraded and not first_failed and not never:
            return "ok"
        if ok:
            return "partial"      # some usable statistics; failures show old/unknown
        if degraded:
            return "degraded"     # previously succeeded, this cycle failing
        if first_failed:
            return "first_failed" # attempted, never succeeded
        return "waiting"
    # No metric rows yet. Attempted-but-not-succeeded is a first failure, not "waiting".
    if snapshot and snapshot.attempted_at is not None and snapshot.sampled_at is None:
        return "first_failed"
    return "waiting"


def _backward_metrics(now, interval, snapshot):
    """Read-only mapping of a pre-migration group snapshot to metric views."""
    cache = {}
    data = snapshot.data if isinstance(snapshot.data, dict) else {}
    for name in data:
        if snapshot.sampled_at is not None:
            state = "degraded" if snapshot.error else "ok"
            last_success = snapshot.sampled_at
        else:
            state = "first_failed"
            last_success = None
        row = _MetricBackfill(name, data[name], last_success, snapshot.attempted_at, state)
        cache[name] = _metric_view(now, interval, name, row)
    return cache


class _MetricBackfill:
    """Minimal stand-in used for legacy mapping (value + timestamps + marker)."""

    def __init__(self, name, value, last_success_at, last_attempt_at, state):
        self.name = name
        self.value = value
        self.last_success_at = last_success_at
        self.last_attempt_at = last_attempt_at
        self.error = None if state == "ok" else ERROR_LEGACY
        self.consecutive_failures = 0
        self.next_attempt_at = None


ERROR_LEGACY = "旧快照映射：历史上采集失败，当前显示旧值。"


async def read_overview(db, days=7):
    now = datetime.now(timezone.utc)
    rows = (await db.scalars(select(MonitoringSnapshot).limit(3))).all()
    by_kind = {row.kind: row for row in rows}
    metric_rows = (await db.scalars(select(MonitoringMetrics).limit(_METRIC_READ_LIMIT))).all()
    by_metric = {}
    for row in metric_rows:
        by_metric.setdefault(row.kind, {})[row.metric] = row

    sections = {}
    for kind, interval in INTERVALS.items():
        snapshot = by_kind.get(kind)
        age = max(0, int((now - aware(snapshot.sampled_at)).total_seconds())) if snapshot and snapshot.sampled_at else None
        base = {
            "data": snapshot.data if snapshot else None,
            "sampled_at": snapshot.sampled_at if snapshot else None,
            "attempted_at": snapshot.attempted_at if snapshot else None,
            "error": snapshot.error if snapshot else None,
            "age_seconds": age,
            "stale": age is None or age > interval * 3,
            "interval_seconds": interval,
        }
        rows_for_kind = by_metric.get(kind, {})
        if rows_for_kind:
            names = METRIC_NAMES.get(kind) or list(rows_for_kind.keys())
            metrics = [_metric_view(now, interval, name, rows_for_kind.get(name)) for name in names]
        elif snapshot and (snapshot.data or snapshot.attempted_at is not None):
            # Backward read-only mapping of a pre-migration group snapshot.
            metrics = list(_backward_metrics(now, interval, snapshot).values())
        else:
            metrics = []
        # Per-kind staleness is derived from per-metric ages, so a single newest
        # success must never mask other old metrics.
        base["stale"] = any(m["stale"] for m in metrics) if metrics else (age is None or age > interval * 3)
        sections[kind] = {**base, "metrics": metrics, "state": _kind_state(metrics, snapshot)}

    history = (await db.scalars(select(MonitoringHistory)
        .where(MonitoringHistory.hour >= now - timedelta(days=days))
        .order_by(MonitoringHistory.hour.desc()).limit(days * 24 + 1))).all()
    return {"scope": "global", "sections": sections,
            "history": [{"at": row.hour, **row.data} for row in reversed(history)]}
