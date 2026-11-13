import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Literal

import anthropic
import structlog
from anthropic.types.beta import (
    BetaAssistantMessageParam,
    BetaMessageParam,
    BetaUserMessageParam,
)
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from redis.exceptions import RedisError

from app.ai.llm.client import AssistantDisabled, get_claude
from app.api.deps import CurrentUser
from app.core.config import get_settings
from app.pubsub import get_redis
from app.services.ai_service import chat

router = APIRouter(tags=["ai"])

RATE_KEY = "ai:chat:{user_id}:{hour}"

log = structlog.get_logger()


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: Annotated[str, Field(min_length=1, max_length=8000)]


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(
        min_length=1,
        max_length=40,
        description="Уся розмова, старші першими; останнє повідомлення — від user",
    )

    @field_validator("messages")
    @classmethod
    def ends_with_user(cls, messages: list[ChatMessage]) -> list[ChatMessage]:
        if messages[0].role != "user" or messages[-1].role != "user":
            raise ValueError("Розмова має починатися й закінчуватися повідомленням user")
        return messages


async def _within_limit(user_id: int) -> bool:
    """Не більше ASSISTANT_MESSAGES_PER_HOUR запитів на годину. Без Redis — без обмеження."""
    limit = get_settings().assistant_messages_per_hour
    key = RATE_KEY.format(user_id=user_id, hour=datetime.now(UTC).strftime("%Y%m%d%H"))
    try:
        redis = get_redis()
        used = await redis.incr(key)
        await redis.expire(key, 3600)
    except (RedisError, OSError):
        return True
    return int(used) <= limit


def _sse(event: str, data: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


async def _stream(history: list[BetaMessageParam]) -> AsyncIterator[str]:
    try:
        async for event in chat(history):
            yield _sse(event.kind, event.data)
    except anthropic.RateLimitError:
        yield _sse("error", {"detail": "Асистент перевантажений, спробуйте за хвилину"})
    except anthropic.APIStatusError as exc:
        log.error("assistant.api_error", status=exc.status_code, request_id=exc.request_id)
        yield _sse("error", {"detail": "Асистент тимчасово недоступний"})
    except anthropic.APIConnectionError:
        yield _sse("error", {"detail": "Немає зв'язку з асистентом, спробуйте пізніше"})


@router.post(
    "/ai/chat",
    summary="AI-асистент (Claude): відповідь потоком SSE",
    description=(
        "Асистент відповідає про P2P-ринок на живих даних сервісу: ціни бірж, оголошення, "
        "арбітражні можливості, калькулятор угоди, історію цін. Цифри бере лише з інструментів.\n\n"
        "Відповідь — text/event-stream з подіями: text {text} — шматок відповіді; "
        "tool {name, input} — асистент перевіряє дані; done {model, stop_reason, токени}; "
        "error {detail}. Розмову зберігає клієнт і щоразу надсилає її повністю.\n\n"
        "Потрібен вхід; не більше ASSISTANT_MESSAGES_PER_HOUR запитів на годину (429)."
    ),
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def assistant_chat(body: ChatRequest, user: CurrentUser) -> StreamingResponse:
    try:
        get_claude()
    except AssistantDisabled as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    if not await _within_limit(user.id):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Ліміт повідомлень асистенту на цю годину вичерпано",
        )
    history: list[BetaMessageParam] = [
        BetaUserMessageParam(role="user", content=message.content)
        if message.role == "user"
        else BetaAssistantMessageParam(role="assistant", content=message.content)
        for message in body.messages
    ]
    return StreamingResponse(
        _stream(history),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
