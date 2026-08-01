"""Add disposable PDF previews for document versions.

Revision ID: 0023
Revises: 0022
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "document_versions",
        sa.Column("preview_blob_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_document_versions_preview_blob_id_blobs",
        "document_versions",
        "blobs",
        ["preview_blob_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_document_versions_preview_blob_id",
        "document_versions",
        ["preview_blob_id"],
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
                SELECT d.id, dv.id, 'preview', 'created',
                       dv.id::text || ':preview:pdf-v1', 0, 1, 'pdf-v1'
                FROM documents d
                JOIN document_versions dv ON dv.id = d.current_version_id
                JOIN blobs b ON b.id = dv.blob_id
                WHERE d.is_deleted IS FALSE
                  AND (
                    lower(coalesce(b.original_filename, '')) LIKE '%%.doc'
                    OR lower(coalesce(b.original_filename, '')) LIKE '%%.docx'
                  )
                ON CONFLICT (idempotency_key) DO NOTHING
                """
            )
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM processing_jobs "
        "WHERE stage = 'preview' AND config_version = 'pdf-v1'"
    )
    op.drop_index(
        "ix_document_versions_preview_blob_id",
        table_name="document_versions",
    )
    op.drop_constraint(
        "fk_document_versions_preview_blob_id_blobs",
        "document_versions",
        type_="foreignkey",
    )
    op.drop_column("document_versions", "preview_blob_id")
