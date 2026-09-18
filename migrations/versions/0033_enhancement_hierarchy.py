"""Optional overview nodes, no historical enqueue or config changes."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0033"
down_revision = "0032"
branch_labels = depends_on = None


def upgrade():
    data = sa.JSON().with_variant(JSONB(), "postgresql")
    op.create_table("knowledge_enhancement_nodes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("knowledge_enhancement_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("node_key", sa.String(40), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.Integer(), nullable=False),
        sa.Column("window_stop", sa.Integer(), nullable=False),
        sa.Column("children", data, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("result", data),
        sa.Column("model_identity", data),
        sa.Column("last_error", sa.Text()),
        sa.UniqueConstraint("run_id", "node_key", name="uq_enhancement_node_key"))
    for column in ("id", "run_id"):
        op.create_index(f"ix_knowledge_enhancement_nodes_{column}", "knowledge_enhancement_nodes", [column])


def downgrade():
    op.drop_table("knowledge_enhancement_nodes")
