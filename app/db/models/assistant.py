"""Розмови з AI-асистентом: зберігаються на сервері, щоб історія була на будь-якому пристрої."""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Identity, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class AssistantConversation(TimestampMixin, Base):
    __tablename__ = "assistant_conversations"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(120))
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


Index(
    "ix_assistant_conversations_user_id_updated_at",
    AssistantConversation.user_id,
    AssistantConversation.updated_at.desc(),
)


class AssistantMessage(TimestampMixin, Base):
    __tablename__ = "assistant_messages"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("assistant_conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    tool_calls: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    actions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")
