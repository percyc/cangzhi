"""Queue PDF previews for existing WebDAV Word documents.

Revision ID: 0024
Revises: 0023
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    bind.execute(
        sa.text(
            """
            INSERT INTO processing_jobs (
                document_id, document_version_id, stage, status,
                idempotency_key, retry_count, max_retries, config_version
            )
            SELECT d.id, dv.id, 'preview', 'created',
                   dv.id::text || ':preview:pdf-webdav-v1',
                   0, 1, 'pdf-webdav-v1'
            FROM documents d
            JOIN document_versions dv ON dv.id = d.current_version_id
            WHERE d.is_deleted IS FALSE
              AND dv.preview_blob_id IS NULL
              AND d.meta->>'external_source' = 'webdav'
              AND (
                lower(coalesce(d.meta->>'webdav_path', '')) LIKE '%%.doc'
                OR lower(coalesce(d.meta->>'webdav_path', '')) LIKE '%%.docx'
              )
            ON CONFLICT (idempotency_key) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM processing_jobs "
        "WHERE stage = 'preview' AND config_version = 'pdf-webdav-v1'"
    )
