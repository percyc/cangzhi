from sqlalchemy import Boolean, Column, Enum, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.schema import UniqueConstraint
from sqlalchemy import text
from ..core.db import Base
from .base import BaseModel
import enum


class DocumentSourceType(enum.Enum):
    url = "url"
    file = "file"
    note = "note"


class Document(BaseModel):
    __tablename__ = "documents"

    title = Column(String(1024), nullable=False, index=True)
    description = Column(Text, nullable=True)
    source_type = Column(Enum(DocumentSourceType), nullable=False)
    source_url = Column(String(2048), nullable=True)
    current_version_id = Column(
        Integer,
        ForeignKey(
            "document_versions.id",
            name="fk_documents_current_version_id_document_versions",
            use_alter=True,
        ),
        nullable=True,
    )
    is_deleted = Column(Boolean, nullable=False, default=False, server_default=text("false"), index=True)
    meta = Column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
        server_default=text("'{}'"),
    )


class DocumentVersion(BaseModel):
    __tablename__ = "document_versions"

    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False, index=True)
    blob_id = Column(Integer, ForeignKey("blobs.id"), nullable=True, index=True)
    version_number = Column(Integer, nullable=False)
    content_hash = Column(String(64), nullable=False)
    raw_content = Column(Text, nullable=True)
    structured_content = Column(JSON().with_variant(JSONB, "postgresql"), nullable=True)
    processing_status = Column(
        String(20), nullable=False, default="created", server_default=text("'created'")
    )
    meta = Column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
        server_default=text("'{}'"),
    )

    __table_args__ = (
        UniqueConstraint("document_id", "version_number", name="uix_document_version"),
    )
