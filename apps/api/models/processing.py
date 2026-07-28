from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import text
from ..core.db import Base
from .base import BaseModel


class ProcessingJob(BaseModel):
    __tablename__ = "processing_jobs"

    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False, index=True)
    document_version_id = Column(Integer, ForeignKey("document_versions.id"), nullable=False, index=True)
    stage = Column(String(50), nullable=False)
    status = Column(String(20), nullable=False, default="created", server_default=text("'created'"))

    # Idempotency key = document_version_id:stage:config_hash
    idempotency_key = Column(String(128), nullable=False, unique=True, index=True)

    retry_count = Column(Integer, nullable=False, default=0, server_default=text("0"))
    max_retries = Column(Integer, nullable=False, default=3, server_default=text("3"))
    next_retry_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    error_details = Column(JSON().with_variant(JSONB, "postgresql"), nullable=True)
    config_version = Column(String(64), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
