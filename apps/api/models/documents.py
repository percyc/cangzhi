from sqlalchemy import Column, String, Text, Integer, Enum, Boolean, ForeignKey
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
    meta = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))


class DocumentVersion(BaseModel):
    __tablename__ = "document_versions"

    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False, index=True)
    version_number = Column(Integer, nullable=False)
    content_hash = Column(String(64), nullable=False)
    raw_content = Column(Text, nullable=True)
    structured_content = Column(JSONB, nullable=True)
    processing_status = Column(
        String(20), nullable=False, default="created", server_default=text("'created'")
    )
    meta = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))

    __table_args__ = (
        UniqueConstraint("document_id", "version_number", name="uix_document_version"),
    )
