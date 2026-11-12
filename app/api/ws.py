"""WebSocket /ws/market: live-оновлення знімків і можливостей по підписаних парах.

Клієнт → сервер: {"action": "subscribe" | "unsubscribe", "pairs": [{"asset", "fiat"}],
"types": ["snapshot", "opportunity"]}; без types — обидва типи, "*" — будь-який актив або фіат.
Сервер → клієнт: події {"type", "ts", "data"} з market:updates, а на кожну команду —
"subscribed" з усіма поточними підписками або "error". Початкові дані — з REST.

{"action": "auth", "token": "<access-токен>"} — вхід у з'єднанні (браузер не передає
заголовок Authorization у WebSocket); після "authenticated" сюди йдуть події alert власника.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Literal

import anyio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import AfterValidator, AwareDatetime, BaseModel, Field, TypeAdapter, ValidationError

from app.core.security import InvalidToken, decode_token
from app.db.models import User, UserStatus
from app.db.repositories.users import get_user
from app.db.session import SessionLocal
from app.domain import EVENT_TYPES, EventType
from app.services.market_stream import MarketClient, MarketHub

PAIR_CODE_PATTERN = r"^(\*|[A-Za-z0-9]{1,16})$"
MAX_SUBSCRIPTIONS = 100
LAGGING_CLOSE_CODE = 1013

router = APIRouter()
hub = MarketHub()

PairCode = Annotated[str, Field(pattern=PAIR_CODE_PATTERN), AfterValidator(str.upper)]


class PairRef(BaseModel):
    asset: PairCode
    fiat: PairCode


class SubscriptionCommand(BaseModel):
    action: Literal["subscribe", "unsubscribe"]
    pairs: list[PairRef] = Field(min_length=1, max_length=MAX_SUBSCRIPTIONS)
    types: list[EventType] = Field(default=list(EVENT_TYPES), min_length=1)


class AuthCommand(BaseModel):
    action: Literal["auth"]
    token: Annotated[str, Field(min_length=1, max_length=2048)]


COMMAND: TypeAdapter[SubscriptionCommand | AuthCommand] = TypeAdapter(
    Annotated[SubscriptionCommand | AuthCommand, Field(discriminator="action")]
)


class SubscriptionItem(BaseModel):
    type: EventType
    asset: str
    fiat: str


class SubscriptionsData(BaseModel):
    subscriptions: list[SubscriptionItem]


class ErrorData(BaseModel):
    detail: str


class AuthData(BaseModel):
    user_id: int
    username: str


ReplyType = Literal["subscribed", "authenticated", "error"]
ReplyData = SubscriptionsData | AuthData | ErrorData


class ServerReply(BaseModel):
    type: ReplyType
    ts: AwareDatetime
    data: ReplyData


def _reply(kind: ReplyType, data: ReplyData) -> str:
    return ServerReply(type=kind, ts=datetime.now(UTC), data=data).model_dump_json()


def _subscribed(client: MarketClient) -> str:
    items = [
        SubscriptionItem(type=kind, asset=asset, fiat=fiat)
        for kind, asset, fiat in sorted(client.subscriptions)
    ]
    return _reply("subscribed", SubscriptionsData(subscriptions=items))


def _error(detail: str) -> str:
    return _reply("error", ErrorData(detail=detail))


async def load_user(user_id: int) -> User | None:
    async with SessionLocal() as session:
        return await get_user(session, user_id)


async def _authenticate(client: MarketClient, token: str) -> str:
    try:
        claims = decode_token(token, "access")
    except InvalidToken:
        return _error("Сесія закінчилась або токен недійсний")
    user = await load_user(claims.user_id)
    if user is None or user.status != UserStatus.ACTIVE:
        return _error("Акаунт недоступний")
    client.user_id = user.id
    return _reply("authenticated", AuthData(user_id=user.id, username=user.username))


async def handle_command(client: MarketClient, text: str | None) -> str:
    """Застосовує команду клієнта. Повертає відповідь для клієнта."""
    if text is None:
        return _error("Очікується JSON-команда в текстовому повідомленні")
    try:
        command = COMMAND.validate_json(text)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(map(str, error['loc'])) or 'json'}: {error['msg']}"
            for error in exc.errors(include_url=False)
        )
        return _error(f"Невірна команда: {problems}")

    if isinstance(command, AuthCommand):
        return await _authenticate(client, command.token)
    keys = {(kind, pair.asset, pair.fiat) for pair in command.pairs for kind in command.types}
    if command.action == "unsubscribe":
        client.subscriptions -= keys
    elif len(client.subscriptions | keys) > MAX_SUBSCRIPTIONS:
        return _error(f"Забагато підписок: максимум {MAX_SUBSCRIPTIONS} (пара × тип події)")
    else:
        client.subscriptions |= keys
    return _subscribed(client)


async def _receive(websocket: WebSocket, client: MarketClient) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        client.push(await handle_command(client, message.get("text")))


async def _send(websocket: WebSocket, client: MarketClient) -> None:
    """Єдиний, хто пише в сокет: і події, і відповіді на команди йдуть через чергу клієнта."""
    while (message := await client.queue.get()) is not None:
        try:
            await websocket.send_text(message)
        except WebSocketDisconnect:
            return


SocketPart = Callable[[WebSocket, MarketClient], Awaitable[None]]


@router.websocket("/ws/market")
async def market_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    client = hub.connect()
    try:
        async with anyio.create_task_group() as group:

            async def run_until_done(part: SocketPart) -> None:
                await part(websocket, client)
                group.cancel_scope.cancel()

            group.start_soon(run_until_done, _receive)
            group.start_soon(run_until_done, _send)
    finally:
        hub.disconnect(client)
    if client.lagging:
        await websocket.close(LAGGING_CLOSE_CODE, "Клієнт не встигає отримувати оновлення")
