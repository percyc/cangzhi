from __future__ import annotations

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from .base import BaseModel


class AskConversation(BaseModel):
    """A cloud-synced Q&A record, not a model memory container."""

    __tablename__ = "ask_conversations"

    workspace_id = Column(
        Integer,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        server_default=text("1"),
        index=True,
    )

    admin_id = Column(
        Integer,
        ForeignKey(
            "admins.id",
            name="fk_ask_conversations_admin_id_admins",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    title = Column(String(255), nullable=False, server_default=text("'新问答'"))
    last_asked_at = Column(DateTime(timezone=True), nullable=True, index=True)

    __table_args__ = (
        Index("ix_ask_conversations_admin_last_asked", "admin_id", "last_asked_at"),
    )


class AskTurn(BaseModel):
    """Immutable snapshot of one independently retrieved answer."""

    __tablename__ = "ask_turns"

    conversation_id = Column(
        Integer,
        ForeignKey(
            "ask_conversations.id",
            name="fk_ask_turns_conversation_id_ask_conversations",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    question = Column(Text, nullable=False)
    mode = Column(String(16), nullable=False, server_default=text("'quick'"))
    context_label = Column(String(512), nullable=True)
    scope_snapshot = Column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        server_default=text("'{}'"),
    )
    response_snapshot = Column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_ask_turns_conversation_created", "conversation_id", "created_at"),
    )
