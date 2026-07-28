"""Add categories, tags, summaries and source_url for URL ingestion

Revision ID: 0003
Revises: 52fbbf06e489
Create Date: 2026-07-28 16:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "52fbbf06e489"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


DEFAULT_CATEGORIES = (
    ("work", "工作与项目", 10),
    ("industry", "行业与商业", 20),
    ("tech", "技术与产品", 30),
    ("legal", "法律与政策", 40),
    ("finance", "财务与投资", 50),
    ("culture", "人文与社会", 60),
    ("growth", "个人成长", 70),
    ("life", "生活与健康", 80),
    ("inspiration", "灵感与随手记", 90),
    ("inbox", "待整理", 100),
)


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("source_url", sa.String(length=2048), nullable=True),
    )

    op.create_table(
        "categories",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["categories.id"],
            name="fk_categories_parent_id_categories",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uix_categories_slug"),
    )
    op.create_index(op.f("ix_categories_slug"), "categories", ["slug"], unique=True)
    op.create_index(op.f("ix_categories_parent_id"), "categories", ["parent_id"], unique=False)
    op.create_index(op.f("ix_categories_id"), "categories", ["id"], unique=False)

    op.create_table(
        "tags",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uix_tags_slug"),
    )
    op.create_index(op.f("ix_tags_slug"), "tags", ["slug"], unique=True)
    op.create_index(op.f("ix_tags_id"), "tags", ["id"], unique=False)

    op.create_table(
        "document_categories",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("document_version_id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False, server_default=sa.text("'model'")),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_document_categories_document_id_documents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            name="fk_document_categories_version_id_document_versions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
            name="fk_document_categories_category_id_categories",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_version_id", "category_id", name="uix_document_version_category"
        ),
    )
    op.create_index(op.f("ix_document_categories_document_id"), "document_categories", ["document_id"], unique=False)
    op.create_index(op.f("ix_document_categories_document_version_id"), "document_categories", ["document_version_id"], unique=False)
    op.create_index(op.f("ix_document_categories_category_id"), "document_categories", ["category_id"], unique=False)
    op.create_index(op.f("ix_document_categories_id"), "document_categories", ["id"], unique=False)

    op.create_table(
        "document_tags",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("document_version_id", sa.Integer(), nullable=False),
        sa.Column("tag_id", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False, server_default=sa.text("'model'")),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_document_tags_document_id_documents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            name="fk_document_tags_version_id_document_versions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tag_id"],
            ["tags.id"],
            name="fk_document_tags_tag_id_tags",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_version_id", "tag_id", name="uix_document_version_tag"
        ),
    )
    op.create_index(op.f("ix_document_tags_document_id"), "document_tags", ["document_id"], unique=False)
    op.create_index(op.f("ix_document_tags_document_version_id"), "document_tags", ["document_version_id"], unique=False)
    op.create_index(op.f("ix_document_tags_tag_id"), "document_tags", ["tag_id"], unique=False)
    op.create_index(op.f("ix_document_tags_id"), "document_tags", ["id"], unique=False)

    op.create_table(
        "document_summaries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("document_version_id", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False, server_default=sa.text("'model'")),
        sa.Column("extra", postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_document_summaries_document_id_documents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            name="fk_document_summaries_version_id_document_versions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_version_id", name="uix_document_summaries_version"),
    )
    op.create_index(op.f("ix_document_summaries_document_id"), "document_summaries", ["document_id"], unique=False)
    op.create_index(op.f("ix_document_summaries_document_version_id"), "document_summaries", ["document_version_id"], unique=False)
    op.create_index(op.f("ix_document_summaries_id"), "document_summaries", ["id"], unique=False)

    # Seed the default top-level categories so the system is usable right away.
    bind = op.get_bind()
    categories_table = sa.table(
        "categories",
        sa.column("slug", sa.String),
        sa.column("name", sa.String),
        sa.column("sort_order", sa.Integer),
        sa.column("is_default", sa.Boolean),
    )
    op.bulk_insert(
        categories_table,
        [
            {
                "slug": slug,
                "name": name,
                "sort_order": order,
                "is_default": True,
            }
            for slug, name, order in DEFAULT_CATEGORIES
        ],
    )


def downgrade() -> None:
    op.drop_table("document_summaries")
    op.drop_table("document_tags")
    op.drop_table("document_categories")
    op.drop_table("tags")
    op.drop_table("categories")
    op.drop_column("documents", "source_url")
