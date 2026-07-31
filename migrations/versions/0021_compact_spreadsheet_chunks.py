"""Rebuild spreadsheet chunks as compact dataset catalogs.

Revision ID: 0021
Revises: 0020
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

CONFIG_VERSION = "chunking:m3-v7-dataset-catalog"


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
            SELECT DISTINCT d.document_id, d.document_version_id,
                   'chunking', 'created',
                   d.document_version_id::text || ':chunking:' || :config_version,
                   0, 3, :config_version
            FROM knowledge_datasets d
            JOIN documents doc ON doc.id = d.document_id
            WHERE doc.current_version_id = d.document_version_id
              AND doc.is_deleted IS FALSE
            ON CONFLICT (idempotency_key) DO NOTHING
            """
        ),
        {"config_version": CONFIG_VERSION},
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM processing_jobs "
        f"WHERE stage = 'chunking' AND config_version = '{CONFIG_VERSION}'"
    )
