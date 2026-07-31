"""Add persistent spreadsheet rows and enqueue table-index backfill.

Revision ID: 0019
Revises: 0018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "structured_table_rows",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("document_version_id", sa.Integer(), nullable=False),
        sa.Column("sheet_name", sa.String(length=256), nullable=False),
        sa.Column("region_index", sa.Integer(), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column(
            "values", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_version_id",
            "sheet_name",
            "region_index",
            "row_number",
            name="uq_table_rows_version_sheet_region_row",
        ),
    )
    op.create_index(
        "ix_structured_table_rows_document_id", "structured_table_rows", ["document_id"]
    )
    op.create_index(
        "ix_structured_table_rows_document_version_id",
        "structured_table_rows",
        ["document_version_id"],
    )
    op.create_index(
        "ix_table_rows_version_dataset",
        "structured_table_rows",
        ["document_version_id", "sheet_name", "region_index"],
    )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                """
                INSERT INTO processing_jobs (
                    document_id, document_version_id, stage, status,
                    idempotency_key, retry_count, max_retries, config_version
                )
                SELECT d.id, v.id, 'chunking', 'created',
                       CAST(v.id AS text) || ':chunking:chunking:m3-v5',
                       0, 3, 'chunking:m3-v5'
                FROM documents d
                JOIN document_versions v ON v.id = d.current_version_id
                WHERE COALESCE(
                    v.structured_content->'metadata'->>'spreadsheet_schema_version',
                    ''
                ) = '2'
                ON CONFLICT (idempotency_key) DO NOTHING
                """
            )
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM processing_jobs "
        "WHERE stage = 'chunking' AND config_version = 'chunking:m3-v5'"
    )
    op.drop_table("structured_table_rows")
