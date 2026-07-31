from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
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
from sqlalchemy.dialects.postgresql import JSONB

from .base import BaseModel

_JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")


class WebDAVSource(BaseModel):
    """Encrypted, read-only connection to an external WebDAV tree."""

    __tablename__ = "webdav_sources"

    name = Column(String(255), nullable=False)
    base_url = Column(String(1024), nullable=False)
    username = Column(String(255), nullable=False)
    password_cipher = Column(Text, nullable=False)
    has_password = Column(Boolean, nullable=False, server_default=text("true"))
    root_path = Column(String(2048), nullable=False, server_default=text("'/'"))
    recursive = Column(Boolean, nullable=False, server_default=text("true"))
    trusted_private_network = Column(
        Boolean, nullable=False, server_default=text("false")
    )
    include_extensions = Column(_JSON_TYPE, nullable=False)
    ignore_patterns = Column(_JSON_TYPE, nullable=False)
    remote_delete_policy = Column(
        String(16), nullable=False, server_default=text("'trash'")
    )
    is_enabled = Column(Boolean, nullable=False, server_default=text("true"), index=True)
    sync_status = Column(
        String(32), nullable=False, server_default=text("'idle'"), index=True
    )
    last_error = Column(Text, nullable=True)
    last_scan_at = Column(DateTime(timezone=True), nullable=True)
    last_sync_at = Column(DateTime(timezone=True), nullable=True)

    def to_public_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "base_url": self.base_url,
            "username": self.username,
            "has_password": bool(self.has_password),
            "root_path": self.root_path,
            "recursive": bool(self.recursive),
            "trusted_private_network": bool(self.trusted_private_network),
            "include_extensions": list(self.include_extensions or []),
            "ignore_patterns": list(self.ignore_patterns or []),
            "remote_delete_policy": self.remote_delete_policy or "trash",
            "is_enabled": bool(self.is_enabled),
            "sync_status": self.sync_status,
            "last_error": self.last_error,
            "last_scan_at": self.last_scan_at.isoformat()
            if self.last_scan_at
            else None,
            "last_sync_at": self.last_sync_at.isoformat()
            if self.last_sync_at
            else None,
        }


class WebDAVEntry(BaseModel):
    """Last observed state of one remote file."""

    __tablename__ = "webdav_entries"

    source_id = Column(
        Integer,
        ForeignKey("webdav_sources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    remote_path = Column(String(2048), nullable=False)
    external_identity = Column(String(64), nullable=True, index=True)
    etag = Column(String(512), nullable=True)
    last_modified = Column(String(255), nullable=True)
    file_size = Column(Integer, nullable=True)
    content_hash = Column(String(64), nullable=True)
    content_type = Column(String(255), nullable=True)
    document_id = Column(
        Integer,
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    state = Column(
        String(32), nullable=False, server_default=text("'discovered'"), index=True
    )
    resume_state = Column(String(32), nullable=True)
    ignore_reason = Column(String(64), nullable=True)
    ignored_at = Column(DateTime(timezone=True), nullable=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    missing_since = Column(DateTime(timezone=True), nullable=True)
    missing_count = Column(Integer, nullable=False, server_default=text("0"))
    keep_snapshot = Column(Boolean, nullable=False, server_default=text("false"))
    synced_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "source_id", "remote_path", name="uix_webdav_entries_source_path"
        ),
        Index("ix_webdav_entries_source_state", "source_id", "state"),
    )


class ExternalItemExclusion(BaseModel):
    """Durable opt-out that survives connector removal and recreation."""

    __tablename__ = "external_item_exclusions"

    source_type = Column(String(32), nullable=False, server_default=text("'webdav'"))
    external_identity = Column(String(64), nullable=False, unique=True, index=True)
    reason = Column(String(64), nullable=False)
    display_path = Column(String(2048), nullable=True)
    source_snapshot = Column(_JSON_TYPE, nullable=False, server_default=text("'{}'"))
