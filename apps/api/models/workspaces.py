"""Workspace model: the container that scopes knowledge and data.

A workspace groups categories, documents and settings under one
slug. The ``default`` workspace always exists and cannot be
archived; additional workspaces are opt-in isolation boundaries.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from .base import BaseModel

DEFAULT_WORKSPACE_SLUG = "default"

_WORKSPACE_STATUS_VALUES = ("active", "archived")


class WorkspaceError(ValueError):
    """Base workspace error carrying a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Workspace(BaseModel):
    __tablename__ = "workspaces"

    slug = Column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
    )
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    is_default = Column(Boolean, nullable=False, server_default=text("false"))
    status = Column(
        String(16),
        nullable=False,
        server_default=text("'active'"),
        index=True,
    )
    settings = Column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        server_default=text("'{}'"),
    )
    created_by_admin_id = Column(
        Integer,
        ForeignKey(
            "admins.id",
            name="fk_workspaces_created_by_admin_id_admins",
            ondelete="SET NULL",
        ),
        nullable=True,
        index=True,
    )

    __table_args__ = (
        UniqueConstraint("slug", name="uix_workspaces_slug"),
        CheckConstraint(
            f"status in {_WORKSPACE_STATUS_VALUES}",
            name="ck_workspaces_status",
        ),
    )

    def to_public_dict(self) -> dict:
        return {
            "id": self.id,
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "is_default": bool(self.is_default),
            "status": self.status,
            "settings": self.settings or {},
            "created_by_admin_id": self.created_by_admin_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }