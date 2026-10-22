"""Small, global administrator-only operational snapshots; never document content."""
from sqlalchemy import Column, DateTime, JSON, Integer, String
from ..core.db import Base


class MonitoringSnapshot(Base):
    __tablename__ = "monitoring_snapshots"
    kind = Column(String(24), primary_key=True)
    data = Column(JSON, nullable=True)
    sampled_at = Column(DateTime(timezone=True), nullable=True)
    attempted_at = Column(DateTime(timezone=True), nullable=False)
    error = Column(String(160), nullable=True)


class MonitoringMetrics(Base):
    """Per-metric operational state, isolated so one failing SQL never hides others.

    Retained on failure: ``value`` and ``last_success_at`` keep the last good
    measurement; a metric never attempted has ``last_attempt_at``/``last_success_at``
    NULL. ``next_attempt_at`` is the persisted, bounded and jittered retry time so
    restart/multi-process restarts do not create a retry storm. ``error`` is a
    sanitized category, never raw SQL, connection or document content.
    """

    __tablename__ = "monitoring_metrics"
    kind = Column(String(24), primary_key=True)
    metric = Column(String(48), primary_key=True)
    value = Column(JSON, nullable=True)
    last_success_at = Column(DateTime(timezone=True), nullable=True)
    last_attempt_at = Column(DateTime(timezone=True), nullable=True)
    error = Column(String(160), nullable=True)
    consecutive_failures = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)


class MonitoringHistory(Base):
    __tablename__ = "monitoring_history"
    hour = Column(DateTime(timezone=True), primary_key=True)
    data = Column(JSON, nullable=False)