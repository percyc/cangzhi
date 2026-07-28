from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

from ..core.db import Base
from .base import BaseModel


# PostgreSQL gets a real JSONB column for JSON-shaped fields. SQLite
# (used in tests) stores the same payload as plain JSON text.
_METADATA_TYPE = JSON().with_variant(JSONB(), "postgresql")


class DocumentChunk(BaseModel):
    """Structure-prioritized parent/child chunk for retrieval."""

    __tablename__ = "document_chunks"

    document_id = Column(
        Integer,
        ForeignKey(
            "documents.id",
            name="fk_document_chunks_document_id_documents",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    document_version_id = Column(
        Integer,
        ForeignKey(
            "document_versions.id",
            name="fk_document_chunks_version_id_document_versions",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    parent_id = Column(
        Integer,
        ForeignKey(
            "document_chunks.id",
            name="fk_document_chunks_parent_id_document_chunks",
            ondelete="CASCADE",
        ),
        nullable=True,
        index=True,
    )
    external_id = Column(String(128), nullable=False)
    role = Column(String(16), nullable=False)
    chunk_type = Column(String(32), nullable=False)
    order_index = Column(Integer, nullable=False, server_default="0")
    content = Column(Text, nullable=False)
    search_text = Column(Text, nullable=False)
    content_hash = Column(String(64), nullable=False)
    heading_path = Column(_METADATA_TYPE, nullable=True)
    page = Column(Integer, nullable=True)
    paragraph_index = Column(Integer, nullable=True)
    source_start = Column(Integer, nullable=True)
    source_end = Column(Integer, nullable=True)
    char_count = Column(Integer, nullable=False, server_default="0")
    token_estimate = Column(Integer, nullable=False, server_default="0")
    language = Column(String(16), nullable=True)
    extra = Column(_METADATA_TYPE, nullable=True)
    is_current = Column(
        Boolean, nullable=False, server_default="true", index=True
    )

    __table_args__ = (
        UniqueConstraint(
            "document_version_id",
            "external_id",
            name="uix_document_chunks_version_external",
        ),
        CheckConstraint(
            "role in ('parent','child')", name="ck_document_chunks_role"
        ),
        CheckConstraint(
            "char_count >= 0", name="ck_document_chunks_char_count"
        ),
        Index(
            "ix_document_chunks_version_order",
            "document_version_id",
            "order_index",
        ),
        Index(
            "ix_document_chunks_parent_order",
            "parent_id",
            "order_index",
        ),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "document_id": self.document_id,
            "document_version_id": self.document_version_id,
            "parent_id": self.parent_id,
            "external_id": self.external_id,
            "role": self.role,
            "chunk_type": self.chunk_type,
            "order_index": self.order_index,
            "content": self.content,
            "content_hash": self.content_hash,
            "heading_path": list(self.heading_path or []),
            "page": self.page,
            "paragraph_index": self.paragraph_index,
            "source_start": self.source_start,
            "source_end": self.source_end,
            "char_count": self.char_count,
            "token_estimate": self.token_estimate,
            "language": self.language,
            "is_current": self.is_current,
        }
