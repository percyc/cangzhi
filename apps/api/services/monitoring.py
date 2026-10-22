"""Read snapshots only: page refresh never performs live counts or filesystem work."""
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from ..models.monitoring import MonitoringHistory, MonitoringSnapshot

INTERVALS = {"queue": 60, "knowledge": 900, "capacity": 900}


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


async def read_overview(db, days=7):
    now = datetime.now(timezone.utc)
    rows = (await db.scalars(select(MonitoringSnapshot).limit(3))).all()
    by_kind = {row.kind: row for row in rows}
    sections = {}
    for kind, interval in INTERVALS.items():
        row = by_kind.get(kind)
        age = max(0, int((now - aware(row.sampled_at)).total_seconds())) if row and row.sampled_at else None
        sections[kind] = {
            "data": row.data if row else None,
            "sampled_at": row.sampled_at if row else None,
            "attempted_at": row.attempted_at if row else None,
            "error": row.error if row else None,
            "age_seconds": age,
            "stale": age is None or age > interval * 3,
            "interval_seconds": interval,
        }
    history = (await db.scalars(select(MonitoringHistory)
        .where(MonitoringHistory.hour >= now - timedelta(days=days))
        .order_by(MonitoringHistory.hour.desc()).limit(days * 24 + 1))).all()
    return {"scope": "global", "sections": sections,
            "history": [{"at": row.hour, **row.data} for row in reversed(history)]}
