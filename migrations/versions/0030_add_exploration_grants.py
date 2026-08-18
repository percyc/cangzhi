"""Add short-lived MCP exploration grants.

Revision ID: 0030
Revises: 0029
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "exploration_grants",
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("admin_id", sa.Integer(), nullable=False),
        sa.Column("created_by_pat_id", sa.Integer(), nullable=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=14), nullable=False),
        sa.Column("scope_keys", json_type, nullable=False),
        sa.Column("document_ids", json_type, nullable=False),
        sa.Column("scopes", json_type, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "id", sa.Integer(), autoincrement=True, nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_exploration_grants_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["admin_id"],
            ["admins.id"],
            name="fk_exploration_grants_admin_id_admins",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_pat_id"],
            ["personal_access_tokens.id"],
            name="fk_exploration_grants_created_by_pat_id_personal_access_tokens",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_exploration_grants_workspace_id",
        "exploration_grants",
        ["workspace_id"],
    )
    op.create_index(
        "ix_exploration_grants_admin_id", "exploration_grants", ["admin_id"]
    )
    op.create_index(
        "ix_exploration_grants_created_by_pat_id",
        "exploration_grants",
        ["created_by_pat_id"],
    )
    op.create_index(
        "ix_exploration_grants_token_hash",
        "exploration_grants",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_exploration_grants_token_prefix",
        "exploration_grants",
        ["token_prefix"],
    )
    op.create_index(
        "ix_exploration_grants_expires_at",
        "exploration_grants",
        ["expires_at"],
    )
    op.create_index(
        "ix_exploration_grants_workspace_active",
        "exploration_grants",
        ["workspace_id", "revoked_at", "expires_at"],
    )


def downgrade() -> None:
    op.drop_table("exploration_grants")
