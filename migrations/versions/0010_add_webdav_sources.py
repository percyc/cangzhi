"""Add read-only WebDAV sources and remote file inventory.

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-29 22:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "webdav_sources",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("base_url", sa.String(length=1024), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("password_cipher", sa.Text(), nullable=False),
        sa.Column(
            "has_password", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "root_path", sa.String(length=2048), server_default="/", nullable=False
        ),
        sa.Column(
            "recursive", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "trusted_private_network",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("include_extensions", sa.JSON(), nullable=False),
        sa.Column("ignore_patterns", sa.JSON(), nullable=False),
        sa.Column(
            "sync_status", sa.String(length=32), server_default="idle", nullable=False
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_webdav_sources_sync_status", "webdav_sources", ["sync_status"])
    op.create_table(
        "webdav_entries",
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("remote_path", sa.String(length=2048), nullable=False),
        sa.Column("etag", sa.String(length=512), nullable=True),
        sa.Column("last_modified", sa.String(length=255), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("document_id", sa.Integer(), nullable=True),
        sa.Column(
            "state", sa.String(length=32), server_default="discovered", nullable=False
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["source_id"], ["webdav_sources.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_id", "remote_path", name="uix_webdav_entries_source_path"
        ),
    )
    op.create_index("ix_webdav_entries_source_id", "webdav_entries", ["source_id"])
    op.create_index("ix_webdav_entries_document_id", "webdav_entries", ["document_id"])
    op.create_index("ix_webdav_entries_state", "webdav_entries", ["state"])
    op.create_index(
        "ix_webdav_entries_source_state", "webdav_entries", ["source_id", "state"]
    )


def downgrade() -> None:
    op.drop_table("webdav_entries")
    op.drop_table("webdav_sources")
