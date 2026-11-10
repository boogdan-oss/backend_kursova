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


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expires: dict[str, int] = {}
        self.sorted_set: dict[str, float] = {}

    async def set(self, key: str, value: str, ex: int) -> None:
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


class BrokenRedis:
    def __getattr__(self, _: str) -> Any:
        async def fail(*__: Any, **___: Any) -> None:
            raise RedisConnectionError("redis is down")

        return fail
