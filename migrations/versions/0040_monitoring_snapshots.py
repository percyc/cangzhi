"""Bounded operational snapshots and hourly capacity history."""
from alembic import op
import sqlalchemy as sa

revision = "0040"
down_revision = "0039"
branch_labels = depends_on = None


def upgrade():
    op.create_index("ix_processing_jobs_monitor_pending", "processing_jobs", ["status", "document_version_id", "created_at"],
        postgresql_where=sa.text("status IN ('created','retry','processing','failed')"))
    op.create_table("monitoring_snapshots",
        sa.Column("kind", sa.String(24), primary_key=True),
        sa.Column("data", sa.JSON()),
        sa.Column("sampled_at", sa.DateTime(timezone=True)),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error", sa.String(160)))
    op.create_table("monitoring_history",
        sa.Column("hour", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("data", sa.JSON(), nullable=False))


def downgrade():
    op.drop_index("ix_processing_jobs_monitor_pending", table_name="processing_jobs")
    op.drop_table("monitoring_history")
    op.drop_table("monitoring_snapshots")
