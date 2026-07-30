"""Rebuild current documents with context-safe chunking.

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-30 23:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
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
            CAST(d.current_version_id AS VARCHAR) || ':chunking:chunking:m3-v2',
            0,
            3,
            'chunking:m3-v2'
        FROM documents AS d
        JOIN document_versions AS v ON v.id = d.current_version_id
        WHERE d.is_deleted = false
          AND v.structured_content IS NOT NULL
        ON CONFLICT (idempotency_key) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM processing_jobs
        WHERE config_version = 'chunking:m3-v2'
          AND stage = 'chunking'
          AND status IN ('created', 'retry', 'failed')
        """
    )
