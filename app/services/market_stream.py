"""Розсилка подій ринку WebSocket-клієнтам.

Процес API тримає одну підписку на Redis market:updates і роздає кожну подію лише тим
клієнтам, що підписались на її тип і пару. Клієнт, який не встигає читати, відключається,
щоб не гальмувати решту і не накопичувати пам'ять.
"""

import asyncio

import structlog
from pydantic import BaseModel, ValidationError
from redis.exceptions import RedisError

from app.domain import EventType
from app.pubsub import get_redis, listen_events

ANY = "*"
CLIENT_QUEUE_SIZE = 256
RECONNECT_PAUSE_SEC = 5.0

Subscription = tuple[EventType, str, str]

log = structlog.get_logger()


class _EventPair(BaseModel):
    asset: str
    fiat: str


class _EventRoute(BaseModel):
    """Лише поля для маршрутизації: решту події API передає клієнтам як є."""

    type: EventType
    data: _EventPair


class MarketClient:
    """Підписки одного з'єднання і черга повідомлень до нього.

    Підписка — (тип події, asset, fiat); "*" замість asset або fiat означає будь-який.
    None у черзі — клієнт відстав і має бути відключений.
    """

    def __init__(self, queue_size: int = CLIENT_QUEUE_SIZE) -> None:
        self.subscriptions: set[Subscription] = set()
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(queue_size)
        self.lagging = False

    def wants(self, kind: EventType, asset: str, fiat: str) -> bool:
        return any(
            (kind, pair_asset, pair_fiat) in self.subscriptions
            for pair_asset in (asset, ANY)
            for pair_fiat in (fiat, ANY)
        )

    def push(self, message: str) -> bool:
        """False — черга переповнена: старі повідомлення відкинуто, клієнт буде відключений."""
        if self.lagging:
            return False
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            self.lagging = True
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait(None)
            return False
        return True


class MarketHub:
    def __init__(self) -> None:
        self._clients: set[MarketClient] = set()

    @property
    def clients(self) -> int:
        return len(self._clients)

    def connect(self, queue_size: int = CLIENT_QUEUE_SIZE) -> MarketClient:
        client = MarketClient(queue_size)
        self._clients.add(client)
        return client

    def disconnect(self, client: MarketClient) -> None:
        self._clients.discard(client)

    def dispatch(self, raw: str) -> int:
        """Роздає подію з Redis підписаним клієнтам. Повертає кількість отримувачів."""
        try:
            route = _EventRoute.model_validate_json(raw)
        except ValidationError as exc:
            log.warning("ws.bad_event", error=str(exc))
            return 0

        delivered = 0
        for client in list(self._clients):
            if not client.wants(route.type, route.data.asset, route.data.fiat):
                continue
            if client.push(raw):
                delivered += 1
            else:
                self.disconnect(client)
                log.warning("ws.client_lagging", clients=len(self._clients))
        return delivered


async def relay_events(hub: MarketHub) -> None:
    """Слухає market:updates до скасування; після збою Redis підписується знову."""
    while True:
        try:
            async for raw in listen_events(get_redis()):
                hub.dispatch(raw)
        except (RedisError, OSError) as exc:
            log.warning("ws.relay_failed", error=repr(exc))
        await asyncio.sleep(RECONNECT_PAUSE_SEC)
