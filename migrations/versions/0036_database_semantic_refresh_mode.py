"""Add database field-semantics refresh mode."""

from alembic import op
import sqlalchemy as sa


revision = "0036"
down_revision = "0035"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "database_sources",
        sa.Column(
            "semantic_refresh_mode",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'smart'"),
        ),
    )
    op.create_check_constraint(
        "ck_database_sources_semantic_refresh_mode",
        "database_sources",
        "semantic_refresh_mode IN ('smart', 'full')",
    )


def downgrade():
    op.drop_constraint(
        "ck_database_sources_semantic_refresh_mode",
        "database_sources",
        type_="check",
    )
    op.drop_column("database_sources", "semantic_refresh_mode")
