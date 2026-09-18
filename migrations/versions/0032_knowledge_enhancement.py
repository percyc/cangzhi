"""Supplementary knowledge runs and evidence windows. No historical enqueue.

Revision ID: 0032
Revises: 0031
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0032"
down_revision = "0031"
branch_labels = depends_on = None


def upgrade():
    data = sa.JSON().with_variant(JSONB(), "postgresql")
    op.create_table("knowledge_enhancement_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_version_id", sa.Integer(), sa.ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_fingerprint", sa.String(64), nullable=False),
        sa.Column("source_snapshot", data, nullable=False),
        sa.Column("config", data, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("call_budget", sa.Integer(), nullable=False),
        sa.Column("calls_used", sa.Integer(), nullable=False),
        sa.Column("total_windows", sa.Integer(), nullable=False),
        sa.Column("total_source_chars", sa.Integer(), nullable=False),
        sa.Column("active_attempt", sa.String(36)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
    )
    for column in ("id", "workspace_id", "document_id", "document_version_id"):
        op.create_index(f"ix_knowledge_enhancement_runs_{column}", "knowledge_enhancement_runs", [column])
    op.create_table("knowledge_enhancement_windows",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("knowledge_enhancement_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("segments", data, nullable=False),
        sa.Column("source_chars", sa.Integer(), nullable=False),
        sa.Column("result", data),
        sa.Column("model_identity", data),
        sa.Column("last_error", sa.Text()),
        sa.UniqueConstraint("run_id", "ordinal", name="uq_enhancement_window_ordinal"),
    )
    for column in ("id", "run_id"):
        op.create_index(f"ix_knowledge_enhancement_windows_{column}", "knowledge_enhancement_windows", [column])


def downgrade():
    op.drop_table("knowledge_enhancement_windows")
    op.drop_table("knowledge_enhancement_runs")
