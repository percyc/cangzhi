from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.ask_history import AskConversation, AskTurn


class AskHistoryError(RuntimeError):
    pass


async def get_owned_conversation(
    db: AsyncSession, conversation_id: int, admin_id: int
) -> AskConversation:
    conversation = await db.scalar(
        select(AskConversation).where(
            AskConversation.id == conversation_id,
            AskConversation.admin_id == admin_id,
        )
    )
    if conversation is None:
        raise AskHistoryError("问答记录不存在")
    return conversation


async def record_ask_turn(
    db: AsyncSession,
    *,
    conversation_id: int,
    admin_id: int,
    question: str,
    mode: str,
    context_label: str | None,
    scope_snapshot: dict[str, Any],
    response_snapshot: dict[str, Any],
) -> AskTurn:
    conversation = await get_owned_conversation(db, conversation_id, admin_id)
    now = datetime.now(timezone.utc)
    turn = AskTurn(
        conversation_id=conversation.id,
        question=question,
        mode=mode,
        context_label=context_label,
        scope_snapshot=scope_snapshot,
        response_snapshot=response_snapshot,
    )
    db.add(turn)
    conversation.last_asked_at = now
    if conversation.title == "新问答":
        conversation.title = question[:80]
    await db.commit()
    await db.refresh(turn)
    return turn
