"""Add first-class knowledge dataset catalog.

Revision ID: 0020
Revises: 0019
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(JSONB(), "postgresql")
    op.create_table(
        "knowledge_datasets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("document_version_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("sheet_name", sa.String(length=256), nullable=False),
        sa.Column("region_index", sa.Integer(), nullable=False),
        sa.Column("dataset_kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("column_count", sa.Integer(), nullable=False),
        sa.Column("header_row", sa.Integer(), nullable=True),
        sa.Column("source_row_start", sa.Integer(), nullable=True),
        sa.Column("source_row_end", sa.Integer(), nullable=True),
        sa.Column("profile", json_type, nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_version_id",
            "sheet_name",
            "region_index",
            name="uq_dataset_version_sheet_region",
        ),
    )
    op.create_index("ix_knowledge_datasets_document_id", "knowledge_datasets", ["document_id"])
    op.create_index(
        "ix_knowledge_datasets_document_version_id",
        "knowledge_datasets",
        ["document_version_id"],
    )
    op.create_index(
        "ix_datasets_version_sheet",
        "knowledge_datasets",
        ["document_version_id", "sheet_name"],
    )
    op.create_table(
        "dataset_fields",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("dataset_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("inferred_type", sa.String(length=32), nullable=False),
        sa.Column("semantic_role", sa.String(length=64), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("null_count", sa.Integer(), nullable=False),
        sa.Column("distinct_count", sa.Integer(), nullable=True),
        sa.Column("sample_values", json_type, nullable=False),
        sa.Column("statistics", json_type, nullable=False),
        sa.ForeignKeyConstraint(
            ["dataset_id"], ["knowledge_datasets.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dataset_id", "position", name="uq_dataset_field_position"),
    )
    op.create_index("ix_dataset_fields_dataset_id", "dataset_fields", ["dataset_id"])
    op.add_column("structured_table_rows", sa.Column("dataset_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_table_rows_dataset_id",
        "structured_table_rows",
        "knowledge_datasets",
        ["dataset_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_structured_table_rows_dataset_id", "structured_table_rows", ["dataset_id"])

    # Existing table indexes become first-class catalog entries immediately.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("""
            INSERT INTO knowledge_datasets (
                document_id, document_version_id, name, sheet_name,
                region_index, dataset_kind, status, row_count, column_count,
                source_row_start, source_row_end, profile
            )
            SELECT r.document_id, r.document_version_id,
                   r.sheet_name || CASE WHEN r.region_index > 1
                       THEN ' · 数据区域 ' || r.region_index::text ELSE '' END,
                   r.sheet_name, r.region_index, 'table', 'ready', COUNT(*), 0,
                   MIN(r.row_number), MAX(r.row_number),
                   jsonb_build_object('catalog_source', 'migration')
            FROM structured_table_rows r
            GROUP BY r.document_id, r.document_version_id, r.sheet_name, r.region_index
        """))
        bind.execute(sa.text("""
            UPDATE structured_table_rows r
            SET dataset_id = d.id
            FROM knowledge_datasets d
            WHERE d.document_version_id = r.document_version_id
              AND d.sheet_name = r.sheet_name
              AND d.region_index = r.region_index
        """))
        bind.execute(sa.text("""
            INSERT INTO dataset_fields (
                dataset_id, position, name, inferred_type, null_count,
                distinct_count, sample_values, statistics
            )
            SELECT d.id, field.ordinality - 1, field.key, 'unknown', 0, NULL,
                   '[]'::jsonb, jsonb_build_object('profile_status', 'pending')
            FROM knowledge_datasets d
            CROSS JOIN LATERAL (
                SELECT r.values
                FROM structured_table_rows r
                WHERE r.dataset_id = d.id
                ORDER BY r.row_number LIMIT 1
            ) first_row
            CROSS JOIN LATERAL jsonb_object_keys(first_row.values)
                WITH ORDINALITY AS field(key, ordinality)
        """))
        bind.execute(sa.text("""
            UPDATE knowledge_datasets d
            SET column_count = (
                SELECT COUNT(*) FROM dataset_fields f WHERE f.dataset_id = d.id
            )
        """))
        bind.execute(sa.text("""
            INSERT INTO processing_jobs (
                document_id, document_version_id, stage, status,
                idempotency_key, retry_count, max_retries, config_version
            )
            SELECT DISTINCT d.document_id, d.document_version_id,
                   'dataset_catalog', 'created',
                   d.document_version_id::text || ':dataset_catalog:dataset-catalog:v1',
                   0, 3, 'dataset-catalog:v1'
            FROM knowledge_datasets d
            ON CONFLICT (idempotency_key) DO NOTHING
        """))


def downgrade() -> None:
    op.drop_index("ix_structured_table_rows_dataset_id", table_name="structured_table_rows")
    op.drop_constraint("fk_table_rows_dataset_id", "structured_table_rows", type_="foreignkey")
    op.drop_column("structured_table_rows", "dataset_id")
    op.drop_table("dataset_fields")
    op.drop_table("knowledge_datasets")
