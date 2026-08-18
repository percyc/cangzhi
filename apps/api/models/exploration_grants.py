"""Short-lived, server-enforced MCP exploration boundaries."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB

from .base import BaseModel


class ExplorationGrant(BaseModel):
    """Opaque knowledge credential derived from a workspace PAT.

    Only a SHA-256 token hash is persisted. The document selector is immutable;
    current scope-key bindings are evaluated for every MCP request so removing
    a document from a key takes effect immediately.
    """

    __tablename__ = "exploration_grants"

    workspace_id = Column(
        Integer,
        ForeignKey(
            "workspaces.id",
            name="fk_exploration_grants_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    admin_id = Column(
        Integer,
        ForeignKey(
            "admins.id",
            name="fk_exploration_grants_admin_id_admins",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    created_by_pat_id = Column(
        Integer,
        ForeignKey(
            "personal_access_tokens.id",
            name="fk_exploration_grants_created_by_pat_id_personal_access_tokens",
            ondelete="SET NULL",
        ),
        nullable=True,
        index=True,
    )
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    token_prefix = Column(String(14), nullable=False, index=True)
    scope_keys = Column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=list,
    )
    document_ids = Column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=list,
    )
    scopes = Column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=list,
    )
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "ix_exploration_grants_workspace_active",
            "workspace_id",
            "revoked_at",
            "expires_at",
        ),
    )

    @property
    def is_valid(self) -> bool:
        if self.revoked_at is not None:
            return False
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires > datetime.now(tz=timezone.utc)

    def to_audit_dict(self) -> dict:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "token_prefix": self.token_prefix,
            "scope_key_count": len(self.scope_keys or []),
            "document_id_count": len(self.document_ids or []),
            "scopes": list(self.scopes or []),
            "expires_at": self.expires_at.isoformat(),
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


__all__ = ["ExplorationGrant"]
