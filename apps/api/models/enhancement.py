"""Version-pinned supplementary knowledge; never a replacement serving index."""
from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from .base import BaseModel

JSON_DATA = JSON().with_variant(JSONB(), "postgresql")


class EnhancementRun(BaseModel):
    __tablename__ = "knowledge_enhancement_runs"
    workspace_id = Column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    document_version_id = Column(Integer, ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False, index=True)
    source_fingerprint = Column(String(64), nullable=False)
    source_snapshot = Column(JSON_DATA, nullable=False)
    config = Column(JSON_DATA, nullable=False)
    status = Column(String(20), nullable=False, default="queued")
    call_budget = Column(Integer, nullable=False)
    calls_used = Column(Integer, nullable=False, default=0)
    total_windows = Column(Integer, nullable=False)
    total_source_chars = Column(Integer, nullable=False)
    active_attempt = Column(String(36), nullable=True)
    lease_until = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)


class EnhancementWindow(BaseModel):
    __tablename__ = "knowledge_enhancement_windows"
    __table_args__ = (UniqueConstraint("run_id", "ordinal", name="uq_enhancement_window_ordinal"),)
    run_id = Column(Integer, ForeignKey("knowledge_enhancement_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    ordinal = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False, default="pending")
    segments = Column(JSON_DATA, nullable=False)
    source_chars = Column(Integer, nullable=False)
    result = Column(JSON_DATA, nullable=True)
    model_identity = Column(JSON_DATA, nullable=True)
    last_error = Column(Text, nullable=True)
