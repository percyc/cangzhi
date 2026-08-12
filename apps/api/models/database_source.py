"""Workspace-scoped, read-only connections to external relational databases.

A ``DatabaseSource`` stores an encrypted password and exposes a catalog of
tables so the operator can import a read-only snapshot into the knowledge
base. Imported snapshots are tracked in ``DatabaseSnapshot`` so re-importing
the same ``source/schema/table`` updates the document instead of creating
endless duplicates.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)

from .base import BaseModel

_ENGINE_VALUES = (
    "postgresql",
    "mysql",
)


class DatabaseSource(BaseModel):
    """Encrypted, read-only connection to a PostgreSQL or MySQL instance."""

    __tablename__ = "database_sources"

    workspace_id = Column(
        Integer,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        server_default=text("1"),
        index=True,
    )

    name = Column(String(255), nullable=False)
    engine = Column(String(32), nullable=False)
    host = Column(String(255), nullable=False)
    port = Column(Integer, nullable=False)
    database_name = Column(String(255), nullable=False)
    username = Column(String(255), nullable=False)
    password_cipher = Column(Text, nullable=False)
    has_password = Column(Boolean, nullable=False, server_default=text("false"))
    ssl_mode = Column(String(32), nullable=False, server_default=text("'prefer'"))
    trusted_private_network = Column(
        Boolean, nullable=False, server_default=text("false")
    )
    is_enabled = Column(
        Boolean, nullable=False, server_default=text("true"), index=True
    )
    status = Column(
        String(32), nullable=False, server_default=text("'idle'"), index=True
    )
    last_error = Column(Text, nullable=True)
    last_tested_at = Column(DateTime(timezone=True), nullable=True)
    last_sync_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "engine IN ('postgresql', 'mysql')",
            name="ck_database_sources_engine",
        ),
    )

    def to_public_dict(self) -> dict:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "name": self.name,
            "engine": self.engine,
            "host": self.host,
            "port": self.port,
            "database_name": self.database_name,
            "username": self.username,
            "has_password": bool(self.has_password),
            "ssl_mode": self.ssl_mode,
            "trusted_private_network": bool(self.trusted_private_network),
            "is_enabled": bool(self.is_enabled),
            "status": self.status,
            "last_error": self.last_error,
            "last_tested_at": self.last_tested_at.isoformat()
            if self.last_tested_at
            else None,
            "last_sync_at": self.last_sync_at.isoformat()
            if self.last_sync_at
            else None,
        }


class DatabaseSnapshot(BaseModel):
    """One imported read-only snapshot of a remote table.

    ``source_id + schema_name + table_name`` is the stable identity used to
    update an existing document instead of creating a new one on re-import.
    """

    __tablename__ = "database_snapshots"

    workspace_id = Column(
        Integer,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        server_default=text("1"),
        index=True,
    )
    source_id = Column(
        Integer,
        ForeignKey("database_sources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    schema_name = Column(String(255), nullable=False)
    table_name = Column(String(255), nullable=False)
    document_id = Column(
        Integer,
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dataset_id = Column(
        Integer,
        ForeignKey("knowledge_datasets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    row_count = Column(Integer, nullable=False, default=0)
    snapshot_fingerprint = Column(String(64), nullable=True)
    snapshot_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "schema_name",
            "table_name",
            name="uix_database_snapshot_source_schema_table",
        ),
        Index(
            "ix_database_snapshots_source",
            "source_id",
            "schema_name",
            "table_name",
        ),
    )


__all__ = [
    "_ENGINE_VALUES",
    "DatabaseSnapshot",
    "DatabaseSource",
]
