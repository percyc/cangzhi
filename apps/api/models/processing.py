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
    # --- ADR-035: task ownership / lease / recovery (migration 0043).
    # Every claim that participates in recovery stamps a unique
    # ``attempt_id`` and a finite ``lease_until`` + ``hard_deadline`` so a
    # supervised executor can be held to a bounded lifetime and a stale
    # attempt can never commit after the lease has been handed to a
    # successor. ``worker_id`` is the stable per-process executor identity;
    # ``attempt_count`` is the bounded recovery counter (distinct from the
    # application ``retry_count`` so a crash-recovery attempt is not silently
    # charged to a model/parse retry budget). Rows written before migration
    # 0043 keep all of these NULL: a ``processing`` row without a lease is a
    # historical orphan and is intentionally never taken over.
    worker_id = Column(String(64), nullable=True, index=True)
    attempt_id = Column(String(64), nullable=True, index=True)
    lease_until = Column(DateTime(timezone=True), nullable=True)
    heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    hard_deadline = Column(DateTime(timezone=True), nullable=True)
    attempt_count = Column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # A persisted flag so an attempt whose child exit could not be confirmed is
    # excluded from the recovery/claim pool until an operator clears it. A plain
    # expired lease is *not* an exit proof; without this, the unconfirmed job
    # would be reclaimed while its old process may still run (double execution).
    quarantined_at = Column(DateTime(timezone=True), nullable=True, index=True)
    recovery_count = Column(Integer, nullable=False, default=0, server_default=text("0"))
