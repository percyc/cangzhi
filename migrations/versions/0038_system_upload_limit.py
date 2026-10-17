"""Persist the global runtime upload limit, preserving environment defaults."""
from alembic import op
import sqlalchemy as sa

revision = "0038"
down_revision = "0037"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "system_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("max_upload_size_mb", sa.Integer(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_system_config_singleton"),
        sa.CheckConstraint("max_upload_size_mb > 0", name="ck_system_config_upload_positive"),
    )


def downgrade():
    op.drop_table("system_config")
