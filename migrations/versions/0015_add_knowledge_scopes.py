"""Add knowledge scopes and personal access tokens.

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-31 10:04:00
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_scopes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "filter",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_knowledge_scopes_id"), "knowledge_scopes", ["id"], unique=False
    )
    op.create_unique_constraint(
        "uix_knowledge_scopes_slug", "knowledge_scopes", ["slug"]
    )

    # Add personal_access_tokens table
    op.create_table(
        "personal_access_tokens",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "admin_id",
            sa.Integer(),
            sa.ForeignKey(
                "admins.id",
                name="fk_personal_access_tokens_admin_id_admins",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=15), nullable=False),
        sa.Column(
            "scopes",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_personal_access_tokens_admin_id"),
        "personal_access_tokens",
        ["admin_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_personal_access_tokens_token_hash"),
        "personal_access_tokens",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        op.f("ix_personal_access_tokens_token_prefix"),
        "personal_access_tokens",
        ["token_prefix"],
        unique=False,
    )
    op.create_index(
        "ix_personal_access_tokens_admin_active",
        "personal_access_tokens",
        ["admin_id", "revoked_at"],
        unique=False,
    )
    op.create_index(
        "ix_personal_access_tokens_expires_at",
        "personal_access_tokens",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_personal_access_tokens_expires_at", table_name="personal_access_tokens"
    )
    op.drop_index(
        "ix_personal_access_tokens_admin_active", table_name="personal_access_tokens"
    )
    op.drop_index(
        op.f("ix_personal_access_tokens_token_prefix"),
        table_name="personal_access_tokens",
    )
    op.drop_index(
        op.f("ix_personal_access_tokens_token_hash"),
        table_name="personal_access_tokens",
    )
    op.drop_index(
        op.f("ix_personal_access_tokens_admin_id"),
        table_name="personal_access_tokens",
    )
    op.drop_table("personal_access_tokens")

    op.drop_constraint(
        "uix_knowledge_scopes_slug", "knowledge_scopes", type_="unique"
    )
    op.drop_index(op.f("ix_knowledge_scopes_id"), table_name="knowledge_scopes")
    op.drop_table("knowledge_scopes")
