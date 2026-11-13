"""Розмови з AI-асистентом. Кожен запит обмежений власником."""

import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AssistantConversation, AssistantMessage


async def list_conversations(
    session: AsyncSession, user_id: int, limit: int
) -> list[AssistantConversation]:
    query = (
        select(AssistantConversation)
        .where(AssistantConversation.user_id == user_id)
        .order_by(AssistantConversation.updated_at.desc())
        .limit(limit)
    )
    return list((await session.execute(query)).scalars())


async def get_conversation(
    session: AsyncSession, user_id: int, conversation_id: str
) -> AssistantConversation | None:
    try:
        uuid.UUID(conversation_id)
    except ValueError:
        return None
    query = select(AssistantConversation).where(
        AssistantConversation.id == conversation_id, AssistantConversation.user_id == user_id
    )
    return (await session.execute(query)).scalars().first()


async def messages(
    session: AsyncSession, conversation_id: str, limit: int
) -> list[AssistantMessage]:
    """Останні limit повідомлень розмови, старші першими."""
    query = (
        select(AssistantMessage)
        .where(AssistantMessage.conversation_id == conversation_id)
        .order_by(AssistantMessage.id.desc())
        .limit(limit)
    )
    return list(reversed(list((await session.execute(query)).scalars())))


async def create_conversation(session: AsyncSession, user_id: int, title: str) -> str:
    conversation_id = str(uuid.uuid4())
    session.add(AssistantConversation(id=conversation_id, user_id=user_id, title=title))
    await session.flush()
    return conversation_id


async def add_message(
    session: AsyncSession,
    conversation_id: str,
    role: str,
    content: str,
    tool_calls: list[str] | None = None,
    actions: list[dict[str, Any]] | None = None,
) -> AssistantMessage:
    message = AssistantMessage(
        conversation_id=conversation_id,
        role=role,
        content=content,
        tool_calls=tool_calls or [],
        actions=actions or [],
    )
    session.add(message)
    await session.flush()
    await session.refresh(message)
    return message


async def touch(session: AsyncSession, conversation_id: str) -> None:
    await session.execute(
        update(AssistantConversation)
        .where(AssistantConversation.id == conversation_id)
        .values(updated_at=func.now())
    )


async def delete_conversation(session: AsyncSession, user_id: int, conversation_id: str) -> bool:
    found = await get_conversation(session, user_id, conversation_id)
    if found is None:
        return False
    await session.delete(found)
    return True
