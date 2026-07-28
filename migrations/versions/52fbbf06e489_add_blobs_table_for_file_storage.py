"""Add blobs table for file storage

Revision ID: 52fbbf06e489
Revises: 0001
Create Date: 2026-07-28 14:05:27.240026

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '52fbbf06e489'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "blobs",
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(length=64), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("original_filename", sa.String(length=1024), nullable=True),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key"),
    )
    op.create_index(op.f("ix_blobs_id"), "blobs", ["id"], unique=False)
    op.create_index(op.f("ix_blobs_sha256"), "blobs", ["sha256"], unique=True)
    op.add_column(
        "document_versions",
        sa.Column("blob_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_document_versions_blob_id_blobs",
        "document_versions",
        "blobs",
        ["blob_id"],
        ["id"],
    )
    op.create_index(
        op.f("ix_document_versions_blob_id"),
        "document_versions",
        ["blob_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_document_versions_blob_id"),
        table_name="document_versions",
    )
    op.drop_constraint(
        "fk_document_versions_blob_id_blobs",
        "document_versions",
        type_="foreignkey",
    )
    op.drop_column("document_versions", "blob_id")
    op.drop_index(op.f("ix_blobs_sha256"), table_name="blobs")
    op.drop_index(op.f("ix_blobs_id"), table_name="blobs")
    op.drop_table("blobs")
