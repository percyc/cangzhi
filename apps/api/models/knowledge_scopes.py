from __future__ import annotations

from sqlalchemy import (
    JSON,
    Column,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from .base import BaseModel

_JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")


class KnowledgeScope(BaseModel):
    """Saved knowledge scope definition for retrieval and Q&A."""

    __tablename__ = "knowledge_scopes"

    name = Column(String(255), nullable=False)
    slug = Column(String(128), nullable=False)
    description = Column(Text, nullable=True)
    filter = Column(_JSON_TYPE, nullable=False, server_default=text("'{}'"))
    version = Column(
        Integer, nullable=False, default=1, server_default=text("1")
    )

    __table_args__ = (
        UniqueConstraint("slug", name="uix_knowledge_scopes_slug"),
    )

    def to_public_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "slug": self.slug,
            "description": self.description,
            "filter": dict(self.filter or {}),
            "version": self.version,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
