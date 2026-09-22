"""Add database snapshot freshness policies."""

from alembic import op
import sqlalchemy as sa


revision = "0035"
down_revision = "0034"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "database_sources",
        sa.Column(
            "freshness_mode",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'background'"),
        ),
    )
    op.add_column(
        "database_sources",
        sa.Column(
            "freshness_interval_minutes",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1440"),
        ),
    )
    op.create_check_constraint(
        "ck_database_sources_freshness_mode",
        "database_sources",
        "freshness_mode IN ('manual', 'background', 'strict')",
    )
    op.create_check_constraint(
        "ck_database_sources_freshness_interval",
        "database_sources",
        "freshness_interval_minutes BETWEEN 5 AND 43200",
    )


def downgrade():
    op.drop_constraint(
        "ck_database_sources_freshness_interval",
        "database_sources",
        type_="check",
    )
    op.drop_constraint(
        "ck_database_sources_freshness_mode",
        "database_sources",
        type_="check",
    )
    op.drop_column("database_sources", "freshness_interval_minutes")
    op.drop_column("database_sources", "freshness_mode")
