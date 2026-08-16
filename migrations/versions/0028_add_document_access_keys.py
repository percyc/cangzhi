"""Bind PATs to workspaces and add document access keys.

Revision ID: 0028
Revises: 0027
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("personal_access_tokens", sa.Column("workspace_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_personal_access_tokens_workspace_id_workspaces",
        "personal_access_tokens", "workspaces", ["workspace_id"], ["id"], ondelete="CASCADE",
    )
    op.create_index(
        "ix_personal_access_tokens_workspace_id", "personal_access_tokens", ["workspace_id"]
    )
    op.create_index(
        "ix_personal_access_tokens_workspace_active",
        "personal_access_tokens", ["workspace_id", "revoked_at"],
    )
    op.create_table(
        "document_access_keys",
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("access_key", sa.String(length=128), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_document_access_keys_workspace_id_workspaces", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], name="fk_document_access_keys_document_id_documents", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "access_key", name="uix_document_access_keys_document_key"),
    )
    op.create_index("ix_document_access_keys_workspace_key_document", "document_access_keys", ["workspace_id", "access_key", "document_id"])
    op.create_index("ix_document_access_keys_document", "document_access_keys", ["document_id"])


def downgrade() -> None:
    op.drop_table("document_access_keys")
    op.drop_index("ix_personal_access_tokens_workspace_active", table_name="personal_access_tokens")
    op.drop_index("ix_personal_access_tokens_workspace_id", table_name="personal_access_tokens")
    op.drop_constraint("fk_personal_access_tokens_workspace_id_workspaces", "personal_access_tokens", type_="foreignkey")
    op.drop_column("personal_access_tokens", "workspace_id")
