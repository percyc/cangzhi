"""Add optional, rebuildable semantic metadata for dataset fields."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0034"
down_revision = "0033"
branch_labels = depends_on = None


def upgrade():
    data = sa.JSON().with_variant(JSONB(), "postgresql")
    op.add_column("dataset_fields", sa.Column("description", sa.String(1000)))
    op.add_column("dataset_fields", sa.Column("unit", sa.String(128)))
    op.add_column(
        "dataset_fields",
        sa.Column("aliases", data, nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column("dataset_fields", sa.Column("semantic_source", sa.String(32)))
    op.add_column("dataset_fields", sa.Column("semantic_confidence", sa.Float()))


def downgrade():
    for column in (
        "semantic_confidence",
        "semantic_source",
        "aliases",
        "unit",
        "description",
    ):
        op.drop_column("dataset_fields", column)
