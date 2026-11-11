import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.domain import Opportunity, Step
from app.services import opportunity_service
from app.services.chain_cache import (
    CHAINS_KEEP,
    CachedChains,
    chains_key,
    read_chains,
    write_chains,
)
from app.services.opportunity_service import CHAINS_FRESH, get_chains
from tests.factories import BrokenRedis, FakeRedis

NOW = datetime.now(UTC)
KEY = "opportunities:chains:UAH:*"


def chain(net: str = "1.5") -> Opportunity:
    return Opportunity(
        kind="chain",
        asset="USDT",
        fiat="UAH",
        buy_exchange="binance",
        sell_exchange="bybit",
        buy_price=Decimal("41.00"),
        sell_price=Decimal("41.80"),
        steps=[Step(exchange="binance", action="BUY", asset="USDT", price=Decimal("41.00"))],
        spread_pct=Decimal("1.9512"),
        net_spread_pct=Decimal(net),
        estimated_profit=Decimal("300.12345678"),
        max_volume=Decimal("5000"),
        detected_at=NOW,
    )


@pytest.mark.parametrize(
    ("investment", "expected"),
    [
        (None, "opportunities:chains:UAH:*"),
        (Decimal("20000"), "opportunities:chains:UAH:20000"),
        (Decimal("20000.00"), "opportunities:chains:UAH:20000"),
        (Decimal("2E+4"), "opportunities:chains:UAH:20000"),
        (Decimal("1500.5"), "opportunities:chains:UAH:1500.5"),
    ],
)
def test_chains_key(investment: Decimal | None, expected: str) -> None:
    assert chains_key("UAH", investment) == expected


async def test_round_trip_keeps_exact_values_and_ttl() -> None:
    redis: Any = FakeRedis()
    cached = CachedChains(computed_at=NOW, items=[chain()])

    await write_chains(redis, KEY, cached)

    assert await read_chains(redis, KEY) == cached
    assert redis.expires[KEY] == CHAINS_KEEP.total_seconds()


async def test_corrupted_value_is_a_miss() -> None:
    redis: Any = FakeRedis()
    redis.values[KEY] = "{not json"

    assert await read_chains(redis, KEY) is None


async def test_redis_outage_is_not_an_error() -> None:
    redis: Any = BrokenRedis()

    await write_chains(redis, KEY, CachedChains(computed_at=NOW, items=[]))

    assert await read_chains(redis, KEY) is None


class ChainSearch:
    """Підміна пошуку ланцюжків: рахує виклики й може тримати їх до сигналу."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, redis: Any) -> None:
        self.calls = 0
        self.result = [chain("2.0")]
        self.release = asyncio.Event()
        self.release.set()

        async def find_markets(*_: Any, **__: Any) -> list[Any]:
            return []

        async def fresh_offers(*_: Any, **__: Any) -> list[Any]:
            await self.release.wait()
            return []

        def chains_for(*_: Any) -> list[Opportunity]:
            self.calls += 1
            return self.result

        monkeypatch.setattr(opportunity_service, "find_markets", find_markets)
        monkeypatch.setattr(opportunity_service, "fresh_offers", fresh_offers)
        monkeypatch.setattr(opportunity_service, "_chains_for", chains_for)
        monkeypatch.setattr(opportunity_service, "get_redis", lambda: redis)


async def settle() -> None:
    for task in list(opportunity_service._chain_refreshes.values()):
        if not task.done():
            await task


async def test_miss_computes_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    redis: Any = FakeRedis()
    search = ChainSearch(monkeypatch, redis)

    assert await get_chains("UAH", None) == search.result
    assert search.calls == 1
    cached = await read_chains(redis, KEY)
    assert cached is not None and cached.items == search.result


async def test_fresh_cache_is_served_without_search(monkeypatch: pytest.MonkeyPatch) -> None:
    redis: Any = FakeRedis()
    search = ChainSearch(monkeypatch, redis)
    await write_chains(redis, KEY, CachedChains(computed_at=datetime.now(UTC), items=[chain()]))

    assert await get_chains("UAH", None) == [chain()]
    assert search.calls == 0


async def test_stale_cache_is_served_and_refreshed_in_background(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis: Any = FakeRedis()
    search = ChainSearch(monkeypatch, redis)
    old = datetime.now(UTC) - CHAINS_FRESH - timedelta(seconds=1)
    await write_chains(redis, KEY, CachedChains(computed_at=old, items=[chain()]))

    assert await get_chains("UAH", None) == [chain()]
    await settle()

    assert search.calls == 1
    refreshed = await read_chains(redis, KEY)
    assert refreshed is not None and refreshed.items == search.result


async def test_concurrent_misses_share_one_search(monkeypatch: pytest.MonkeyPatch) -> None:
    redis: Any = FakeRedis()
    search = ChainSearch(monkeypatch, redis)
    search.release.clear()

    requests = [asyncio.create_task(get_chains("UAH", None)) for _ in range(5)]
    await asyncio.sleep(0)
    search.release.set()
    results = await asyncio.gather(*requests)

    assert search.calls == 1
    assert all(result == search.result for result in results)


async def test_cancelled_request_does_not_cancel_shared_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis: Any = FakeRedis()
    search = ChainSearch(monkeypatch, redis)
    search.release.clear()

    impatient = asyncio.create_task(get_chains("UAH", None))
    await asyncio.sleep(0)
    impatient.cancel()
    await asyncio.gather(impatient, return_exceptions=True)
    search.release.set()
    await settle()

    assert await read_chains(redis, KEY) is not None


async def test_each_investment_has_own_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    redis: Any = FakeRedis()
    search = ChainSearch(monkeypatch, redis)

    await get_chains("UAH", Decimal("20000"))
    await get_chains("UAH", Decimal("50000"))
    await get_chains("UAH", Decimal("20000.00"))

    assert search.calls == 2


async def test_without_redis_chains_are_computed(monkeypatch: pytest.MonkeyPatch) -> None:
    search = ChainSearch(monkeypatch, BrokenRedis())

    assert await get_chains("UAH", None) == search.result
    assert await get_chains("UAH", None) == search.result
    assert search.calls == 2
