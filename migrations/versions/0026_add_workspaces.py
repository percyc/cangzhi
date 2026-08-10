"""Add single-user workspace isolation boundary.

Revision ID: 0026
Revises: 0025

Downgrade requires slugs and external identities to be globally unique again;
it will intentionally fail if multiple workspaces contain duplicates.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

_ROOT_TABLES = (
    "documents",
    "categories",
    "tags",
    "knowledge_scopes",
    "webdav_sources",
    "external_item_exclusions",
    "ask_conversations",
)


def _add_workspace_column(table: str, *, ondelete: str) -> None:
    op.add_column(
        table,
        sa.Column(
            "workspace_id",
            sa.Integer(),
            nullable=True,
            server_default=sa.text("1"),
        ),
    )
    op.execute(
        sa.text(
            f"UPDATE {table} SET workspace_id = "
            "(SELECT id FROM workspaces WHERE slug = 'default')"
        )
    )
    op.alter_column(table, "workspace_id", nullable=False)
    op.create_foreign_key(
        f"fk_{table}_workspace_id_workspaces",
        table,
        "workspaces",
        ["workspace_id"],
        ["id"],
        ondelete=ondelete,
    )
    op.create_index(f"ix_{table}_workspace_id", table, ["workspace_id"])


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "settings", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("created_by_admin_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status in ('active', 'archived')", name="ck_workspaces_status"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_admin_id"],
            ["admins.id"],
            name="fk_workspaces_created_by_admin_id_admins",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uix_workspaces_slug"),
    )
    op.create_index("ix_workspaces_id", "workspaces", ["id"])
    op.create_index("ix_workspaces_slug", "workspaces", ["slug"], unique=True)
    op.create_index("ix_workspaces_status", "workspaces", ["status"])
    op.create_index(
        "ix_workspaces_created_by_admin_id", "workspaces", ["created_by_admin_id"]
    )
    op.execute(
        sa.text(
            "INSERT INTO workspaces "
            "(slug, name, is_default, status, settings) "
            "VALUES ('default', '默认空间', true, 'active', '{}')"
        )
    )

    _add_workspace_column("documents", ondelete="RESTRICT")
    for table in _ROOT_TABLES[1:]:
        _add_workspace_column(table, ondelete="CASCADE")

    op.drop_index("ix_documents_external_identity", table_name="documents")
    op.create_index(
        "ix_documents_external_identity",
        "documents",
        ["external_identity"],
    )
    op.create_unique_constraint(
        "uix_documents_workspace_external_identity",
        "documents",
        ["workspace_id", "external_identity"],
    )

    for table, old_constraint, new_constraint in (
        ("categories", "uix_categories_slug", "uix_categories_workspace_slug"),
        ("tags", "uix_tags_slug", "uix_tags_workspace_slug"),
    ):
        op.drop_constraint(old_constraint, table, type_="unique")
        op.drop_index(f"ix_{table}_slug", table_name=table)
        op.create_index(f"ix_{table}_slug", table, ["slug"])
        op.create_unique_constraint(
            new_constraint, table, ["workspace_id", "slug"]
        )

    op.drop_constraint(
        "uix_knowledge_scopes_slug", "knowledge_scopes", type_="unique"
    )
    op.create_unique_constraint(
        "uix_knowledge_scopes_workspace_slug",
        "knowledge_scopes",
        ["workspace_id", "slug"],
    )

    op.drop_index(
        "ix_external_item_exclusions_external_identity",
        table_name="external_item_exclusions",
    )
    op.create_index(
        "ix_external_item_exclusions_external_identity",
        "external_item_exclusions",
        ["external_identity"],
    )
    op.create_unique_constraint(
        "uix_external_exclusions_workspace_identity",
        "external_item_exclusions",
        ["workspace_id", "external_identity"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uix_external_exclusions_workspace_identity",
        "external_item_exclusions",
        type_="unique",
    )
    op.drop_index(
        "ix_external_item_exclusions_external_identity",
        table_name="external_item_exclusions",
    )
    op.create_index(
        "ix_external_item_exclusions_external_identity",
        "external_item_exclusions",
        ["external_identity"],
        unique=True,
    )

    op.drop_constraint(
        "uix_knowledge_scopes_workspace_slug", "knowledge_scopes", type_="unique"
    )
    op.create_unique_constraint(
        "uix_knowledge_scopes_slug", "knowledge_scopes", ["slug"]
    )

    for table, old_constraint, new_constraint in (
        ("categories", "uix_categories_slug", "uix_categories_workspace_slug"),
        ("tags", "uix_tags_slug", "uix_tags_workspace_slug"),
    ):
        op.drop_constraint(new_constraint, table, type_="unique")
        op.drop_index(f"ix_{table}_slug", table_name=table)
        op.create_index(f"ix_{table}_slug", table, ["slug"], unique=True)
        op.create_unique_constraint(old_constraint, table, ["slug"])

    op.drop_constraint(
        "uix_documents_workspace_external_identity", "documents", type_="unique"
    )
    op.drop_index("ix_documents_external_identity", table_name="documents")
    op.create_index(
        "ix_documents_external_identity",
        "documents",
        ["external_identity"],
        unique=True,
    )

    for table in reversed(_ROOT_TABLES):
        op.drop_index(f"ix_{table}_workspace_id", table_name=table)
        op.drop_constraint(
            f"fk_{table}_workspace_id_workspaces", table, type_="foreignkey"
        )
        op.drop_column(table, "workspace_id")

    op.drop_index("ix_workspaces_created_by_admin_id", table_name="workspaces")
    op.drop_index("ix_workspaces_status", table_name="workspaces")
    op.drop_index("ix_workspaces_slug", table_name="workspaces")
    op.drop_index("ix_workspaces_id", table_name="workspaces")
    op.drop_table("workspaces")
