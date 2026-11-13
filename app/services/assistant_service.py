"""Розмови з AI-асистентом: історія на сервері, відповідь цілим повідомленням.

Відповідь рахує той самий цикл, що й /ai/chat (ai_service.chat). Питання й відповідь
зберігаються разом і лише після успішної відповіді, тож у розмові не лишається питань без
відповіді.
"""

from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AssistantConversation, AssistantMessage, User
from app.db.repositories import assistant as repo
from app.services.ai_service import ChatEvent, ChatTurn, chat

HISTORY_LIMIT = 40
TITLE_LENGTH = 60
CONVERSATIONS_LIMIT = 100

ACTIONS: dict[str, dict[str, str]] = {
    "get_opportunities": {"label": "Показати арбітраж", "to": "/arbitrage"},
    "calculate_deal": {"label": "Відкрити в калькуляторі", "to": "/calculator"},
    "get_offers": {"label": "Відкрити оголошення", "to": "/offers"},
    "get_best_prices": {"label": "Ціни на ринку", "to": "/offers"},
}

Ask = Callable[[Sequence[ChatTurn]], AsyncIterator[ChatEvent]]


class ConversationNotFound(Exception):
    pass


class AssistantFailed(Exception):
    """Асистент повернув подію error: текст — для користувача."""


@dataclass(frozen=True, slots=True)
class ChatResult:
    conversation: AssistantConversation
    user_message: AssistantMessage
    message: AssistantMessage


def title_of(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= TITLE_LENGTH else f"{line[: TITLE_LENGTH - 1]}…"


def with_context(text: str, asset: str | None, fiat: str | None) -> str:
    """Пара, вибрана на сайті, як підказка моделі; у розмові зберігається лише сам текст."""
    if not asset or not fiat:
        return text
    return f"(Пара, вибрана на сайті: {asset.upper()}/{fiat.upper()}.)\n\n{text}"


def actions_for(tools: Sequence[str]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    found = []
    for name in tools:
        action = ACTIONS.get(name)
        if action is not None and action["to"] not in seen:
            seen.add(action["to"])
            found.append(action)
    return found


async def conversations(session: AsyncSession, user: User) -> list[AssistantConversation]:
    return await repo.list_conversations(session, user.id, CONVERSATIONS_LIMIT)


async def conversation(
    session: AsyncSession, user: User, conversation_id: str
) -> tuple[AssistantConversation, list[AssistantMessage]]:
    found = await repo.get_conversation(session, user.id, conversation_id)
    if found is None:
        raise ConversationNotFound("Розмову не знайдено")
    return found, await repo.messages(session, found.id, HISTORY_LIMIT * 5)


async def delete(session: AsyncSession, user: User, conversation_id: str) -> None:
    if not await repo.delete_conversation(session, user.id, conversation_id):
        raise ConversationNotFound("Розмову не знайдено")
    await session.commit()


async def _collect(ask: Ask, turns: Sequence[ChatTurn]) -> tuple[str, list[str]]:
    parts: list[str] = []
    tools: list[str] = []
    async for event in ask(turns):
        if event.kind == "text":
            parts.append(str(event.data.get("text", "")))
        elif event.kind == "tool":
            name = str(event.data.get("name", ""))
            if name and name not in tools:
                tools.append(name)
        elif event.kind == "error":
            raise AssistantFailed(str(event.data.get("detail", "Асистент не зміг відповісти")))
    text = "".join(parts).strip()
    if not text:
        raise AssistantFailed("Асистент не дав відповіді, спробуйте переформулювати питання")
    return text, tools


async def answer(
    session: AsyncSession,
    user: User,
    conversation_id: str | None,
    text: str,
    asset: str | None = None,
    fiat: str | None = None,
    ask: Ask = chat,
) -> ChatResult:
    """Відповідь на нове повідомлення з урахуванням попередніх у розмові."""
    existing = None
    history: list[ChatTurn] = []
    if conversation_id is not None:
        existing = await repo.get_conversation(session, user.id, conversation_id)
        if existing is None:
            raise ConversationNotFound("Розмову не знайдено")
        for item in await repo.messages(session, existing.id, HISTORY_LIMIT):
            history.append(ChatTurn("user" if item.role == "user" else "assistant", item.content))
        while history and history[0].role != "user":
            history.pop(0)
    turns = [*history, ChatTurn("user", with_context(text, asset, fiat))]
    reply, tools = await _collect(ask, turns)

    target = (
        existing.id
        if existing
        else await repo.create_conversation(session, user.id, title_of(text))
    )
    user_message = await repo.add_message(session, target, "user", text)
    message = await repo.add_message(session, target, "assistant", reply, tools, actions_for(tools))
    await repo.touch(session, target)
    await session.commit()
    saved = await repo.get_conversation(session, user.id, target)
    assert saved is not None
    await session.refresh(saved)
    return ChatResult(saved, user_message, message)
