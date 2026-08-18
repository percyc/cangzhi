"""Document-level scope-key bindings.

Each row is one ``(document, scope_key)`` pair. The pair is unique so
a key can be granted to many documents, and a single document can carry
many keys. ``workspace_id`` is denormalised so the scope-key index
can drive an ``EXISTS`` narrowing on top of the workspace-bounded
``Document`` set without re-joining the ``workspaces`` table.

The model deliberately contains no identity, role, or user table.
Scope keys are opaque grouping labels chosen by the upload caller, not
credentials or authorization grants. Workspace access remains solely
the responsibility of ``PersonalAccessToken.workspace_id``. There is
no key registry: a key only exists through the documents referencing it.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)

from .base import BaseModel


class DocumentScopeKey(BaseModel):
    __tablename__ = "document_scope_keys"

    workspace_id = Column(
        Integer,
        ForeignKey(
            "workspaces.id",
            name="fk_document_scope_keys_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    document_id = Column(
        Integer,
        ForeignKey(
            "documents.id",
            name="fk_document_scope_keys_document_id_documents",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    scope_key = Column(
        String(128),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "scope_key",
            name="uix_document_scope_keys_document_key",
        ),
        Index(
            "ix_document_scope_keys_workspace_key_document",
            "workspace_id",
            "scope_key",
            "document_id",
        ),
        Index(
            "ix_document_scope_keys_document",
            "document_id",
        ),
    )

    def to_public_dict(self) -> dict:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "document_id": self.document_id,
            "scope_key": self.scope_key,
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
            "updated_at": (
                self.updated_at.isoformat() if self.updated_at else None
            ),
        }


__all__ = ["DocumentScopeKey"]
