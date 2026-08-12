"""Add read-only database sources and their snapshots.

Revision ID: 0027
Revises: 0026
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def _workspace_fk(table: str, name: str) -> None:
    op.create_foreign_key(
        f"fk_{table}_{name}_workspaces",
        table,
        "workspaces",
        [name],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(f"ix_{table}_{name}", table, [name])


def upgrade() -> None:
    op.create_table(
        "database_sources",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "workspace_id",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("engine", sa.String(length=32), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("database_name", sa.String(length=255), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("password_cipher", sa.Text(), nullable=False),
        sa.Column(
            "has_password", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "ssl_mode",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'prefer'"),
        ),
        sa.Column(
            "trusted_private_network",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'idle'"),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "engine IN ('postgresql', 'mysql')",
            name="ck_database_sources_engine",
        ),
    )
    op.create_index("ix_database_sources_id", "database_sources", ["id"])
    op.create_index("ix_database_sources_status", "database_sources", ["status"])
    op.create_index("ix_database_sources_is_enabled", "database_sources", ["is_enabled"])
    _workspace_fk("database_sources", "workspace_id")

    op.create_table(
        "database_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "workspace_id",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("schema_name", sa.String(length=255), nullable=False),
        sa.Column("table_name", sa.String(length=255), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("dataset_id", sa.Integer(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("snapshot_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_id",
            "schema_name",
            "table_name",
            name="uix_database_snapshot_source_schema_table",
        ),
    )
    op.create_index("ix_database_snapshots_id", "database_snapshots", ["id"])
    _workspace_fk("database_snapshots", "workspace_id")
    op.create_index("ix_database_snapshots_source_id", "database_snapshots", ["source_id"])
    op.create_index(
        "ix_database_snapshots_document_id", "database_snapshots", ["document_id"]
    )
    op.create_index("ix_database_snapshots_dataset_id", "database_snapshots", ["dataset_id"])
    op.create_index(
        "ix_database_snapshots_source",
        "database_snapshots",
        ["source_id", "schema_name", "table_name"],
    )
    op.create_foreign_key(
        "fk_database_snapshots_source_id_database_sources",
        "database_snapshots",
        "database_sources",
        ["source_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_database_snapshots_document_id_documents",
        "database_snapshots",
        "documents",
        ["document_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_database_snapshots_dataset_id_knowledge_datasets",
        "database_snapshots",
        "knowledge_datasets",
        ["dataset_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_table("database_snapshots")
    op.drop_table("database_sources")