import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from redis.exceptions import ConnectionError as RedisConnectionError

from app.domain import Merchant, Offer, Side

FETCHED_AT = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def make_offer(
    price: str = "41.20",
    side: Side = "BUY",
    *,
    merchant_id: str = "m1",
    orders: int = 100,
    completion_rate: str = "0.99",
    min_amount: str = "500",
    max_amount: str = "20000",
    available: str = "100",
    payments: tuple[str, ...] = ("monobank",),
) -> Offer:
    return Offer(
        exchange="binance",
        side=side,
        asset="USDT",
        fiat="UAH",
        price=Decimal(price),
        min_amount=Decimal(min_amount),
        max_amount=Decimal(max_amount),
        available=Decimal(available),
        payment_methods=list(payments),
        merchant=Merchant(
            id=merchant_id,
            nickname=merchant_id,
            orders_30d=orders,
            completion_rate=Decimal(completion_rate),
        ),
        fetched_at=FETCHED_AT,
    )


class FakePubSub:
    def __init__(self, redis: "FakeRedis") -> None:
        self._redis = redis
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def __aenter__(self) -> "FakePubSub":
        return self

    async def __aexit__(self, *_: Any) -> None:
        for queues in self._redis.channels.values():
            if self._queue in queues:
                queues.remove(self._queue)

    async def subscribe(self, channel: str) -> None:
        self._redis.channels.setdefault(channel, []).append(self._queue)
        self._queue.put_nowait({"type": "subscribe", "channel": channel, "data": 1})

    async def listen(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            yield await self._queue.get()


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expires: dict[str, int] = {}
        self.sorted_set: dict[str, float] = {}
        self.published: list[tuple[str, str]] = []
        self.channels: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}

    async def set(self, key: str, value: str, ex: int = 0) -> None:
        self.values[key] = value
        self.expires[key] = ex

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def zadd(self, _: str, mapping: dict[str, float]) -> None:
        self.sorted_set.update(mapping)

    async def zremrangebyscore(self, _: str, __: str, deadline: float) -> None:
        self.sorted_set = {k: v for k, v in self.sorted_set.items() if v > deadline}

    async def zrange(self, _: str, __: int, ___: int) -> list[str]:
        return list(self.sorted_set)

    async def incr(self, key: str) -> int:
        value = int(self.values.get(key, "0")) + 1
        self.values[key] = str(value)
        return value

    async def expire(self, key: str, seconds: int) -> None:
        self.expires[key] = seconds

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)

    async def getdel(self, key: str) -> str | None:
        return self.values.pop(key, None)

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        queues = self.channels.get(channel, [])
        for queue in queues:
            queue.put_nowait({"type": "message", "channel": channel, "data": message})
        return len(queues)

    def pubsub(self) -> FakePubSub:
        return FakePubSub(self)

    async def until_subscribed(self, channel: str) -> None:
        for _ in range(100):
            if self.channels.get(channel):
                return
            await asyncio.sleep(0)
        raise AssertionError(f"ніхто не підписався на {channel}")


class BrokenRedis:
    def __getattr__(self, _: str) -> Any:
        async def fail(*__: Any, **___: Any) -> None:
            raise RedisConnectionError("redis is down")

        return fail

    def pubsub(self) -> "BrokenRedis":
        return self

    async def __aenter__(self) -> "BrokenRedis":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None
