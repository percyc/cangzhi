from __future__ import annotations

from sqlalchemy import (
    JSON,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

from .base import BaseModel

_VALUES_TYPE = JSON().with_variant(JSONB(), "postgresql")


class StructuredTableRow(BaseModel):
    """One addressable data row from a parsed spreadsheet region."""

    __tablename__ = "structured_table_rows"

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
    dataset_id = Column(
        Integer,
        ForeignKey("knowledge_datasets.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    sheet_name = Column(String(256), nullable=False)
    region_index = Column(Integer, nullable=False)
    row_number = Column(Integer, nullable=False)
    values = Column(_VALUES_TYPE, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "document_version_id",
            "sheet_name",
            "region_index",
            "row_number",
            name="uq_table_rows_version_sheet_region_row",
        ),
        Index(
            "ix_table_rows_version_dataset",
            "document_version_id",
            "sheet_name",
            "region_index",
        ),
    )
