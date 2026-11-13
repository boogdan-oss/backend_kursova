"""Сторінка «AI-асистент»: розмови на сервері, відповідь цілим JSON (без потоку)."""

from datetime import datetime
from typing import Annotated, Literal

import anthropic
import structlog
from fastapi import APIRouter, HTTPException, Response, status
from google.genai import errors as gemini_errors
from pydantic import BaseModel, Field, StringConstraints

from app.ai.llm.client import AssistantDisabled, assistant_provider
from app.api.deps import CODE_PATTERN, CurrentUser, SessionDep
from app.api.v1.ai import BUSY, UNAVAILABLE, within_limit
from app.db.models import AssistantConversation, AssistantMessage
from app.services import assistant_service as service
from app.services.ai_service import chat
from app.services.assistant_service import AssistantFailed, ConversationNotFound

router = APIRouter(prefix="/assistant", tags=["assistant"])

log = structlog.get_logger()


class ChatAction(BaseModel):
    label: str
    to: str = Field(description="Шлях сторінки сайту")


class ChatMessage(BaseModel):
    id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime
    tool_calls: list[str] = Field(description="Інструменти, якими асистент перевіряв дані")
    actions: list[ChatAction]


class Conversation(BaseModel):
    id: str
    title: str
    updated_at: datetime


class ConversationsResponse(BaseModel):
    count: int
    items: list[Conversation]


class ConversationDetail(Conversation):
    messages: list[ChatMessage]


class ChatRequest(BaseModel):
    conversation_id: str | None = Field(default=None, description="Без нього — нова розмова")
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
    asset: str | None = Field(default=None, pattern=CODE_PATTERN, description="Пара на сайті")
    fiat: str | None = Field(default=None, pattern=CODE_PATTERN)


class ChatResponse(BaseModel):
    conversation_id: str
    title: str
    user_message: ChatMessage
    message: ChatMessage


def _conversation(item: AssistantConversation) -> Conversation:
    return Conversation(id=item.id, title=item.title, updated_at=item.updated_at)


def _message(item: AssistantMessage) -> ChatMessage:
    return ChatMessage(
        id=str(item.id),
        role="user" if item.role == "user" else "assistant",
        content=item.content,
        created_at=item.created_at,
        tool_calls=list(item.tool_calls or []),
        actions=[ChatAction(**action) for action in item.actions or []],
    )


def _not_found(exc: ConversationNotFound) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))


@router.get("/conversations", response_model=ConversationsResponse, summary="Мої розмови")
async def list_conversations(session: SessionDep, user: CurrentUser) -> ConversationsResponse:
    items = [_conversation(item) for item in await service.conversations(session, user)]
    return ConversationsResponse(count=len(items), items=items)


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetail,
    summary="Розмова з повідомленнями",
)
async def get_conversation(
    conversation_id: str, session: SessionDep, user: CurrentUser
) -> ConversationDetail:
    try:
        found, messages = await service.conversation(session, user, conversation_id)
    except ConversationNotFound as exc:
        raise _not_found(exc) from exc
    return ConversationDetail(
        **_conversation(found).model_dump(), messages=[_message(item) for item in messages]
    )


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Видалити розмову",
)
async def delete_conversation(
    conversation_id: str, session: SessionDep, user: CurrentUser
) -> Response:
    try:
        await service.delete(session, user, conversation_id)
    except ConversationNotFound as exc:
        raise _not_found(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/chat",
    response_model=ChatResponse,
    summary="Повідомлення асистенту: відповідь цілим JSON",
    description=(
        "Той самий фінансовий асистент, що й /ai/chat (Gemini або Claude, живі дані лише з "
        "інструментів), але розмова зберігається на сервері, а відповідь приходить одним JSON. "
        "Без conversation_id починається нова розмова; її назва — початок першого питання. "
        "asset і fiat — пара, вибрана на сайті: підказка асистенту, якщо в питанні пари немає.\n\n"
        "Відповідь може йти 5–60 с. Помилки: 404 — чужа чи неіснуюча розмова, 429 — ліміт "
        "ASSISTANT_MESSAGES_PER_HOUR, 503 — асистент не налаштований чи перевантажений, "
        "502 — асистент не зміг відповісти."
    ),
)
async def send_message(body: ChatRequest, session: SessionDep, user: CurrentUser) -> ChatResponse:
    try:
        assistant_provider()
    except AssistantDisabled as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    if not await within_limit(user.id):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Ліміт повідомлень асистенту на цю годину вичерпано",
        )
    try:
        result = await service.answer(
            session, user, body.conversation_id, body.message, body.asset, body.fiat, ask=chat
        )
    except ConversationNotFound as exc:
        raise _not_found(exc) from exc
    except AssistantFailed as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except gemini_errors.APIError as exc:
        log.error("assistant.gemini_error", status=exc.code, error=exc.status)
        busy = exc.code in (429, 503)
        code = status.HTTP_503_SERVICE_UNAVAILABLE if busy else status.HTTP_502_BAD_GATEWAY
        raise HTTPException(code, BUSY if busy else UNAVAILABLE) from exc
    except anthropic.RateLimitError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, BUSY) from exc
    except anthropic.APIError as exc:
        log.error("assistant.claude_error", error=repr(exc))
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, UNAVAILABLE) from exc
    return ChatResponse(
        conversation_id=result.conversation.id,
        title=result.conversation.title,
        user_message=_message(result.user_message),
        message=_message(result.message),
    )
