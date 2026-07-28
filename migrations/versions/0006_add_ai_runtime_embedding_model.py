"""Add optional embedding model to AI runtime config.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-29 00:30:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_runtime_configs",
        sa.Column("embedding_model", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ai_runtime_configs", "embedding_model")
