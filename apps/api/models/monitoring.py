"""Small, global administrator-only operational snapshots; never document content."""
from sqlalchemy import Column, DateTime, JSON, String
from ..core.db import Base


class MonitoringSnapshot(Base):
    __tablename__ = "monitoring_snapshots"
    kind = Column(String(24), primary_key=True)
    data = Column(JSON, nullable=True)
    sampled_at = Column(DateTime(timezone=True), nullable=True)
    attempted_at = Column(DateTime(timezone=True), nullable=False)
    error = Column(String(160), nullable=True)


class MonitoringHistory(Base):
    __tablename__ = "monitoring_history"
    hour = Column(DateTime(timezone=True), primary_key=True)
    data = Column(JSON, nullable=False)
