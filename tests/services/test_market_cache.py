from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.domain import MarketSnapshot
from app.services.market_cache import (
    SNAPSHOT_TTL_SEC,
    read_snapshot,
    snapshot_key,
    write_snapshot,
)
from tests.factories import BrokenRedis, FakeRedis

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
KEY = "market:snapshot:USDT/UAH:*:*"


def make_snapshot(**changes: Any) -> MarketSnapshot:
    values: dict[str, Any] = {
        "exchange": "binance",
        "asset": "USDT",
        "fiat": "UAH",
        "buy_price": Decimal("41.25"),
        "sell_price": Decimal("41.05"),
        "spread_pct": Decimal("-0.4848"),
        "volume": Decimal("1234.56789"),
        "offers_count_buy": 3,
        "offers_count_sell": 2,
        "fetched_at": NOW,
    }
    return MarketSnapshot(**(values | changes))


@pytest.mark.parametrize(
    ("exchange", "amount", "expected"),
    [
        (None, None, "market:snapshot:USDT/UAH:*:*"),
        ("bybit", None, "market:snapshot:USDT/UAH:bybit:*"),
        (None, Decimal("5000"), "market:snapshot:USDT/UAH:*:5000"),
        (None, Decimal("5000.00"), "market:snapshot:USDT/UAH:*:5000"),
        (None, Decimal("5E+3"), "market:snapshot:USDT/UAH:*:5000"),
        (None, Decimal("2500.5"), "market:snapshot:USDT/UAH:*:2500.5"),
    ],
)
def test_snapshot_key(exchange: str | None, amount: Decimal | None, expected: str) -> None:
    assert snapshot_key("USDT", "UAH", exchange, amount) == expected


async def test_snapshot_round_trip_keeps_exact_decimals() -> None:
    redis: Any = FakeRedis()
    items = [make_snapshot(), make_snapshot(payment_method="monobank", sell_price=None)]

    await write_snapshot(redis, KEY, "v1", items)

    assert await read_snapshot(redis, KEY, "v1") == items
    assert redis.expires[KEY] == SNAPSHOT_TTL_SEC


async def test_empty_snapshot_is_cached_too() -> None:
    redis: Any = FakeRedis()

    await write_snapshot(redis, KEY, "v1", [])

    assert await read_snapshot(redis, KEY, "v1") == []


async def test_other_version_is_a_miss() -> None:
    redis: Any = FakeRedis()
    await write_snapshot(redis, KEY, "v1", [make_snapshot()])

    assert await read_snapshot(redis, KEY, "v2") is None


async def test_missing_key_is_a_miss() -> None:
    assert await read_snapshot(FakeRedis(), KEY, "v1") is None  # type: ignore[arg-type]


async def test_corrupted_value_is_a_miss() -> None:
    redis: Any = FakeRedis()
    redis.values[KEY] = "{not json"

    assert await read_snapshot(redis, KEY, "v1") is None


async def test_redis_outage_is_not_an_error() -> None:
    redis: Any = BrokenRedis()

    await write_snapshot(redis, KEY, "v1", [make_snapshot()])

    assert await read_snapshot(redis, KEY, "v1") is None
