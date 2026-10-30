"""Default new external OCR configurations to 120s; preserve saved values."""
from alembic import op
import sqlalchemy as sa

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("ai_runtime_configs") as batch:
        batch.alter_column("ocr_timeout_seconds", existing_type=sa.Integer(),
                           existing_nullable=False, server_default=sa.text("120"))


def downgrade():
    with op.batch_alter_table("ai_runtime_configs") as batch:
        batch.alter_column("ocr_timeout_seconds", existing_type=sa.Integer(),
                           existing_nullable=False, server_default=sa.text("30"))
