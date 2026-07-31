from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

from .base import BaseModel

_JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")


class KnowledgeDataset(BaseModel):
    """A queryable two-dimensional region owned by a document version."""

    __tablename__ = "knowledge_datasets"

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
    name = Column(String(512), nullable=False)
    sheet_name = Column(String(256), nullable=False)
    region_index = Column(Integer, nullable=False)
    dataset_kind = Column(String(32), nullable=False, default="table")
    status = Column(String(32), nullable=False, default="ready")
    row_count = Column(Integer, nullable=False, default=0)
    column_count = Column(Integer, nullable=False, default=0)
    header_row = Column(Integer, nullable=True)
    source_row_start = Column(Integer, nullable=True)
    source_row_end = Column(Integer, nullable=True)
    profile = Column(_JSON_TYPE, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint(
            "document_version_id",
            "sheet_name",
            "region_index",
            name="uq_dataset_version_sheet_region",
        ),
        Index(
            "ix_datasets_version_sheet",
            "document_version_id",
            "sheet_name",
        ),
    )


class DatasetField(BaseModel):
    """A profiled field in a knowledge dataset."""

    __tablename__ = "dataset_fields"

    dataset_id = Column(
        Integer,
        ForeignKey("knowledge_datasets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    position = Column(Integer, nullable=False)
    name = Column(String(512), nullable=False)
    inferred_type = Column(String(32), nullable=False, default="unknown")
    semantic_role = Column(String(64), nullable=True)
    confidence = Column(Float, nullable=True)
    null_count = Column(Integer, nullable=False, default=0)
    distinct_count = Column(Integer, nullable=True)
    sample_values = Column(_JSON_TYPE, nullable=False, default=list)
    statistics = Column(_JSON_TYPE, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("dataset_id", "position", name="uq_dataset_field_position"),
    )


class DatasetArtifact(BaseModel):
    """A rebuildable columnar snapshot used by the dataset execution layer."""

    __tablename__ = "dataset_artifacts"

    dataset_id = Column(
        Integer,
        ForeignKey("knowledge_datasets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_number = Column(Integer, nullable=False)
    format = Column(String(16), nullable=False, default="parquet")
    status = Column(String(32), nullable=False, default="building")
    storage_key = Column(String(1024), nullable=True)
    checksum = Column(String(64), nullable=True)
    byte_size = Column(Integer, nullable=False, default=0)
    row_count = Column(Integer, nullable=False, default=0)
    schema_snapshot = Column(_JSON_TYPE, nullable=False, default=dict)
    is_active = Column(Boolean, nullable=False, default=False)
    built_at = Column(DateTime(timezone=True), nullable=True)
    error_message = Column(String(2000), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "dataset_id", "version_number", name="uq_dataset_artifact_version"
        ),
        Index("ix_dataset_artifact_active", "dataset_id", "is_active"),
    )
