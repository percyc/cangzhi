"""Add reversible tag merge history.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-30 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "tag_merge_records",
        sa.Column("target_tag_id", sa.Integer(), nullable=True),
        sa.Column("source_snapshot", sa.JSON(), nullable=False),
        sa.Column("moved_version_ids", sa.JSON(), nullable=False),
        sa.Column("deduplicated_version_ids", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("undone_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["target_tag_id"],
            ["tags.id"],
            name="fk_tag_merge_records_target_tag_id_tags",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_tag_merge_records_target_tag_id",
        "tag_merge_records",
        ["target_tag_id"],
    )
    op.create_index(
        "ix_tag_merge_records_status",
        "tag_merge_records",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index("ix_tag_merge_records_status", table_name="tag_merge_records")
    op.drop_index(
        "ix_tag_merge_records_target_tag_id",
        table_name="tag_merge_records",
    )
    op.drop_table("tag_merge_records")
