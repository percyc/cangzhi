"""Add document_chunks table for structure-prioritized retrieval

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-28 19:00:00

This migration introduces the chunk layer described in
``docs/ARCHITECTURE.md`` §3.4 / §4.1 and ADR-004/005:

* ``document_chunks`` stores both parent and child chunks for a
  document version. The parent/child relationship is a self-FK so a
  parent row can refer to its children without a join table.
* Each chunk records its structural position (heading_path, page,
  paragraph_index) and a source span so the UI can deep link back
  to the original document.
* The PostgreSQL search path uses a functional GIN index on
  ``to_tsvector('simple', search_text)`` and a trigram GIN index on
  ``search_text`` (for Chinese substrings). SQLite (used
  in tests) does not create the indexes; the API layer falls back
  to ``LIKE``-based search automatically.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("document_version_id", sa.Integer(), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("chunk_type", sa.String(length=32), nullable=False),
        sa.Column(
            "order_index",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("heading_path", postgresql.JSONB(), nullable=True),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("paragraph_index", sa.Integer(), nullable=True),
        sa.Column("source_start", sa.Integer(), nullable=True),
        sa.Column("source_end", sa.Integer(), nullable=True),
        sa.Column(
            "char_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "token_estimate",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("extra", postgresql.JSONB(), nullable=True),
        sa.Column(
            "is_current",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_document_chunks_document_id_documents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            name="fk_document_chunks_version_id_document_versions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["document_chunks.id"],
            name="fk_document_chunks_parent_id_document_chunks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_version_id",
            "external_id",
            name="uix_document_chunks_version_external",
        ),
        sa.CheckConstraint(
            "role in ('parent','child')",
            name="ck_document_chunks_role",
        ),
        sa.CheckConstraint(
            "char_count >= 0",
            name="ck_document_chunks_char_count",
        ),
    )

    op.create_index(
        op.f("ix_document_chunks_document_id"),
        "document_chunks",
        ["document_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_document_chunks_document_version_id"),
        "document_chunks",
        ["document_version_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_document_chunks_parent_id"),
        "document_chunks",
        ["parent_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_document_chunks_is_current"),
        "document_chunks",
        ["is_current"],
        unique=False,
    )
    op.create_index(
        "ix_document_chunks_version_order",
        "document_chunks",
        ["document_version_id", "order_index"],
        unique=False,
    )
    op.create_index(
        "ix_document_chunks_parent_order",
        "document_chunks",
        ["parent_id", "order_index"],
        unique=False,
    )

    if is_postgres:
        # Enable pg_trgm first so the GIN trigram index below can be
        # created. The extension call is idempotent.
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        # Functional GIN index over to_tsvector('simple', search_text).
        # The expression index keeps writes simple: we only update the
        # search_text column when the chunk content changes.
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_document_chunks_search_vector "
            "ON document_chunks USING GIN (to_tsvector('simple', search_text))"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_document_chunks_search_text_trgm "
            "ON document_chunks USING GIN (search_text gin_trgm_ops)"
        )

    # Existing parsed documents need a one-time chunking job; otherwise
    # search would only work for documents imported after this migration.
    op.execute(
        """
        INSERT INTO processing_jobs (
            document_id,
            document_version_id,
            stage,
            status,
            idempotency_key,
            retry_count,
            max_retries,
            config_version
        )
        SELECT
            d.id,
            d.current_version_id,
            'chunking',
            'created',
            CAST(d.current_version_id AS VARCHAR) || ':chunking:chunking:m3-v1',
            0,
            3,
            'chunking:m3-v1'
        FROM documents AS d
        JOIN document_versions AS v ON v.id = d.current_version_id
        WHERE d.is_deleted = false
          AND v.structured_content IS NOT NULL
        ON CONFLICT (idempotency_key) DO NOTHING
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    if is_postgres:
        op.execute("DROP INDEX IF EXISTS ix_document_chunks_search_vector")
        op.execute("DROP INDEX IF EXISTS ix_document_chunks_search_text_trgm")
    op.drop_index(
        "ix_document_chunks_parent_order", table_name="document_chunks"
    )
    op.drop_index(
        "ix_document_chunks_version_order", table_name="document_chunks"
    )
    op.drop_index(
        op.f("ix_document_chunks_is_current"), table_name="document_chunks"
    )
    op.drop_index(
        op.f("ix_document_chunks_parent_id"), table_name="document_chunks"
    )
    op.drop_index(
        op.f("ix_document_chunks_document_version_id"),
        table_name="document_chunks",
    )
    op.drop_index(
        op.f("ix_document_chunks_document_id"), table_name="document_chunks"
    )
    op.drop_table("document_chunks")
