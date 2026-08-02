"""Add cloud-synced Q&A history without conversational memory.

Revision ID: 0025
Revises: 0024
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "ask_conversations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("admin_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False, server_default=sa.text("'新问答'")),
        sa.Column("last_asked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["admin_id"], ["admins.id"],
            name="fk_ask_conversations_admin_id_admins", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ask_conversations_id"), "ask_conversations", ["id"])
    op.create_index(op.f("ix_ask_conversations_admin_id"), "ask_conversations", ["admin_id"])
    op.create_index(op.f("ix_ask_conversations_last_asked_at"), "ask_conversations", ["last_asked_at"])
    op.create_index("ix_ask_conversations_admin_last_asked", "ask_conversations", ["admin_id", "last_asked_at"])

    op.create_table(
        "ask_turns",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False, server_default=sa.text("'quick'")),
        sa.Column("context_label", sa.String(length=512), nullable=True),
        sa.Column("scope_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("response_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["ask_conversations.id"],
            name="fk_ask_turns_conversation_id_ask_conversations", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ask_turns_id"), "ask_turns", ["id"])
    op.create_index(op.f("ix_ask_turns_conversation_id"), "ask_turns", ["conversation_id"])
    op.create_index("ix_ask_turns_conversation_created", "ask_turns", ["conversation_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_ask_turns_conversation_created", table_name="ask_turns")
    op.drop_index(op.f("ix_ask_turns_conversation_id"), table_name="ask_turns")
    op.drop_index(op.f("ix_ask_turns_id"), table_name="ask_turns")
    op.drop_table("ask_turns")
    op.drop_index("ix_ask_conversations_admin_last_asked", table_name="ask_conversations")
    op.drop_index(op.f("ix_ask_conversations_last_asked_at"), table_name="ask_conversations")
    op.drop_index(op.f("ix_ask_conversations_admin_id"), table_name="ask_conversations")
    op.drop_index(op.f("ix_ask_conversations_id"), table_name="ask_conversations")
    op.drop_table("ask_conversations")
