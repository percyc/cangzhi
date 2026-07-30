from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import text
from ..core.db import Base
from .base import BaseModel


class ProcessingJob(BaseModel):
    __tablename__ = "processing_jobs"

    document_id = Column(
        Integer,
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_version_id = Column(
        Integer,
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
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
    # --- M3-6b phase 2: explicit embedding-profile association.
    # Set on jobs whose ``stage`` is ``embedding`` so the worker
    # can claim "next pending job for this profile" with a single
    # indexed read. Nullable for every legacy stage that does not
    # belong to a profile.
    embedding_profile_id = Column(
        Integer,
        ForeignKey(
            "embedding_profiles.id",
            name="fk_processing_jobs_embedding_profile_embedding_profiles",
            ondelete="CASCADE",
        ),
        nullable=True,
        index=True,
    )
    embedding_chunk_id = Column(
        Integer,
        ForeignKey(
            "document_chunks.id",
            name="fk_processing_jobs_embedding_chunk_document_chunks",
            ondelete="CASCADE",
        ),
        nullable=True,
        index=True,
    )
