"""Rename document access keys to document scope keys.

Revision ID: 0029
Revises: 0028
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.alter_column(
            "document_access_keys",
            "access_key",
            new_column_name="scope_key",
        )
        op.execute(
            "ALTER TABLE document_access_keys RENAME CONSTRAINT "
            "uix_document_access_keys_document_key TO "
            "uix_document_scope_keys_document_key"
        )
        op.execute(
            "ALTER TABLE document_access_keys RENAME CONSTRAINT "
            "fk_document_access_keys_workspace_id_workspaces TO "
            "fk_document_scope_keys_workspace_id_workspaces"
        )
        op.execute(
            "ALTER TABLE document_access_keys RENAME CONSTRAINT "
            "fk_document_access_keys_document_id_documents TO "
            "fk_document_scope_keys_document_id_documents"
        )
        op.alter_column(
            "document_access_keys",
            "scope_key",
            existing_type=sa.String(length=128),
            existing_nullable=False,
        )
        op.execute(
            "ALTER INDEX ix_document_access_keys_workspace_key_document "
            "RENAME TO ix_document_scope_keys_workspace_key_document"
        )
        op.execute(
            "ALTER INDEX ix_document_access_keys_document "
            "RENAME TO ix_document_scope_keys_document"
        )
        op.rename_table("document_access_keys", "document_scope_keys")
    else:
        op.alter_column(
            "document_access_keys",
            "access_key",
            new_column_name="scope_key",
        )
        op.rename_table("document_access_keys", "document_scope_keys")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.rename_table("document_scope_keys", "document_access_keys")
        op.execute(
            "ALTER INDEX ix_document_scope_keys_workspace_key_document "
            "RENAME TO ix_document_access_keys_workspace_key_document"
        )
        op.execute(
            "ALTER INDEX ix_document_scope_keys_document "
            "RENAME TO ix_document_access_keys_document"
        )
        op.execute(
            "ALTER TABLE document_access_keys RENAME CONSTRAINT "
            "uix_document_scope_keys_document_key TO "
            "uix_document_access_keys_document_key"
        )
        op.execute(
            "ALTER TABLE document_access_keys RENAME CONSTRAINT "
            "fk_document_scope_keys_workspace_id_workspaces TO "
            "fk_document_access_keys_workspace_id_workspaces"
        )
        op.execute(
            "ALTER TABLE document_access_keys RENAME CONSTRAINT "
            "fk_document_scope_keys_document_id_documents TO "
            "fk_document_access_keys_document_id_documents"
        )
        op.alter_column(
            "document_access_keys",
            "scope_key",
            new_column_name="access_key",
            existing_type=sa.String(length=128),
            existing_nullable=False,
        )
    else:
        op.rename_table("document_scope_keys", "document_access_keys")
        op.alter_column(
            "document_access_keys",
            "scope_key",
            new_column_name="access_key",
        )
