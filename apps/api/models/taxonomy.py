from sqlalchemy import (
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import text
from sqlalchemy.orm import relationship

from ..core.db import Base
from .base import BaseModel


DEFAULT_CATEGORY_SLUGS: tuple[tuple[str, str], ...] = (
    ("work", "工作与项目"),
    ("industry", "行业与商业"),
    ("tech", "技术与产品"),
    ("legal", "法律与政策"),
    ("finance", "财务与投资"),
    ("culture", "人文与社会"),
    ("growth", "个人成长"),
    ("life", "生活与健康"),
    ("inspiration", "灵感与随手记"),
    ("inbox", "待整理"),
)


class Category(BaseModel):
    __tablename__ = "categories"

    slug = Column(String(64), nullable=False, unique=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    sort_order = Column(Integer, nullable=False, server_default=text("0"))
    is_default = Column(Boolean, nullable=False, server_default=text("false"))
    parent_id = Column(
        Integer,
        ForeignKey("categories.id", name="fk_categories_parent_id_categories"),
        nullable=True,
        index=True,
    )

    parent = relationship("Category", remote_side="Category.id", backref="children")

    __table_args__ = (
        UniqueConstraint("slug", name="uix_categories_slug"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "sort_order": self.sort_order,
            "is_default": self.is_default,
            "parent_id": self.parent_id,
        }


class DocumentCategory(BaseModel):
    __tablename__ = "document_categories"

    document_id = Column(
        Integer,
        ForeignKey("documents.id", name="fk_document_categories_document_id_documents", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_version_id = Column(
        Integer,
        ForeignKey("document_versions.id", name="fk_document_categories_version_id_document_versions", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    category_id = Column(
        Integer,
        ForeignKey("categories.id", name="fk_document_categories_category_id_categories", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    is_primary = Column(Boolean, nullable=False, server_default=text("false"))
    confidence = Column(Float, nullable=True)
    source = Column(String(32), nullable=False, server_default=text("'model'"))
    rationale = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "document_version_id", "category_id", name="uix_document_version_category"
        ),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "document_id": self.document_id,
            "document_version_id": self.document_version_id,
            "category_id": self.category_id,
            "is_primary": self.is_primary,
            "confidence": self.confidence,
            "source": self.source,
            "rationale": self.rationale,
        }


class Tag(BaseModel):
    __tablename__ = "tags"

    slug = Column(String(64), nullable=False, unique=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("slug", name="uix_tags_slug"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
        }


class DocumentTag(BaseModel):
    __tablename__ = "document_tags"

    document_id = Column(
        Integer,
        ForeignKey("documents.id", name="fk_document_tags_document_id_documents", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_version_id = Column(
        Integer,
        ForeignKey("document_versions.id", name="fk_document_tags_version_id_document_versions", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tag_id = Column(
        Integer,
        ForeignKey("tags.id", name="fk_document_tags_tag_id_tags", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    confidence = Column(Float, nullable=True)
    source = Column(String(32), nullable=False, server_default=text("'model'"))

    __table_args__ = (
        UniqueConstraint(
            "document_version_id", "tag_id", name="uix_document_version_tag"
        ),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "document_id": self.document_id,
            "document_version_id": self.document_version_id,
            "tag_id": self.tag_id,
            "confidence": self.confidence,
            "source": self.source,
        }


class DocumentSummary(BaseModel):
    __tablename__ = "document_summaries"

    document_id = Column(
        Integer,
        ForeignKey("documents.id", name="fk_document_summaries_document_id_documents", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_version_id = Column(
        Integer,
        ForeignKey("document_versions.id", name="fk_document_summaries_version_id_document_versions", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    summary = Column(Text, nullable=False)
    model = Column(String(128), nullable=True)
    prompt_version = Column(String(64), nullable=True)
    confidence = Column(Float, nullable=True)
    source = Column(String(32), nullable=False, server_default=text("'model'"))
    extra = Column(JSON().with_variant(JSONB, "postgresql"), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "document_version_id", name="uix_document_summaries_version"
        ),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "document_id": self.document_id,
            "document_version_id": self.document_version_id,
            "summary": self.summary,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "confidence": self.confidence,
            "source": self.source,
        }
