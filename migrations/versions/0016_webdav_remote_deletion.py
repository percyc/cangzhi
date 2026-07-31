"""Add safe WebDAV remote deletion lifecycle.

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-31 15:30:00
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "webdav_sources",
        sa.Column(
            "remote_delete_policy",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'trash'"),
        ),
    )
    op.add_column(
        "webdav_entries",
        sa.Column("missing_since", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "webdav_entries",
        sa.Column(
            "missing_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "webdav_entries",
        sa.Column(
            "keep_snapshot",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Existing missing entries get a fresh grace window after upgrading.
    op.execute(
        sa.text(
            "UPDATE webdav_entries "
            "SET state = 'suspected_missing', missing_count = 1, "
            "missing_since = CURRENT_TIMESTAMP "
            "WHERE state = 'missing'"
        )
    )


def downgrade() -> None:
    op.drop_column("webdav_entries", "keep_snapshot")
    op.drop_column("webdav_entries", "missing_count")
    op.drop_column("webdav_entries", "missing_since")
    op.drop_column("webdav_sources", "remote_delete_policy")
