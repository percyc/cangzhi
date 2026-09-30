"""Record the opt-in remembered browser session mode."""

from alembic import op
import sqlalchemy as sa


revision = "0037"
down_revision = "0036"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "auth_sessions",
        sa.Column("remembered", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade():
    op.drop_column("auth_sessions", "remembered")
