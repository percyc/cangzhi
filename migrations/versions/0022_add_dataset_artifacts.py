"""Add versioned Parquet dataset artifacts and enqueue their initial build.

Revision ID: 0022
Revises: 0021
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

CONFIG_VERSION = "dataset-parquet:v1"


def upgrade() -> None:
    json_type = sa.JSON().with_variant(JSONB(), "postgresql")
    op.create_table(
        "dataset_artifacts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column("dataset_id", sa.Integer(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("format", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("storage_key", sa.String(length=1024), nullable=True),
        sa.Column("checksum", sa.String(length=64), nullable=True),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("schema_snapshot", json_type, nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.String(length=2000), nullable=True),
        sa.ForeignKeyConstraint(
            ["dataset_id"], ["knowledge_datasets.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "dataset_id", "version_number", name="uq_dataset_artifact_version"
        ),
    )
    op.create_index(
        "ix_dataset_artifacts_dataset_id", "dataset_artifacts", ["dataset_id"]
    )
    op.create_index(
        "ix_dataset_artifact_active", "dataset_artifacts", ["dataset_id", "is_active"]
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
                SELECT DISTINCT d.document_id, d.document_version_id,
                       'dataset_artifact', 'created',
                       d.document_version_id::text || ':dataset_artifact:' || :version,
                       0, 3, :version
                FROM knowledge_datasets d
                JOIN documents doc ON doc.id = d.document_id
                WHERE doc.current_version_id = d.document_version_id
                  AND doc.is_deleted IS FALSE
                ON CONFLICT (idempotency_key) DO NOTHING
                """
            ),
            {"version": CONFIG_VERSION},
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM processing_jobs "
        f"WHERE stage = 'dataset_artifact' AND config_version = '{CONFIG_VERSION}'"
    )
    op.drop_index("ix_dataset_artifact_active", table_name="dataset_artifacts")
    op.drop_index("ix_dataset_artifacts_dataset_id", table_name="dataset_artifacts")
    op.drop_table("dataset_artifacts")
