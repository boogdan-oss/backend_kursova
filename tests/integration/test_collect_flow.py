"""Наскрізний тест на справжній БД: біржа (мок) → collector → PostgreSQL → API.

Потрібна окрема порожня БД з TimescaleDB, на яку застосовано міграції:
    TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:15432/crypto_p2p_test pytest
Таблиці очищуються перед кожним тестом, тому робочу БД сюди не підставляти.
"""

import json
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.collectors.base import ExchangeBlocked
from app.collectors.binance import (
    BASE_URL,
    CONFIG_PATH,
    FIAT_LIST_PATH,
    SEARCH_PATH,
    BinanceAdapter,
)
from app.collectors.ratelimit import AdaptiveRateLimiter
from app.collectors.scheduler import CollectTask
from app.db.models import (
    CurrencyPair,
    Exchange,
    ExchangeMarket,
    MarketStats,
    Merchant,
    OfferSnapshot,
    PaymentMethod,
)
from app.db.repositories.market import (
    exchange_overview,
    find_markets,
    get_exchange_id,
    get_or_create_pair,
    market_history,
    markets_listed_at,
    table_stats,
    tracked_pairs,
)
from app.db.repositories.offers import latest_offers, save_offers, upsert_merchants
from app.db.session import get_session
from app.main import app
from app.services import market_service
from app.services.collect_service import MarketStatsTracker, collect_task, refresh_markets
from tests.factories import FakeRedis, make_offer

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL не заданий")

FIXTURES = Path(__file__).parent.parent / "fixtures"
SEARCH_URL = BASE_URL + SEARCH_PATH
BUY = CollectTask("binance", "USDT", "UAH", "BUY")
SELL = CollectTask("binance", "USDT", "UAH", "SELL")
DATA_TABLES = "offer_snapshots, market_stats, exchange_markets, merchants"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def search_responses(request: httpx.Request) -> httpx.Response:
    side = json.loads(request.content)["tradeType"].lower()
    return httpx.Response(200, json=fixture(f"binance_{side}.json"))


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {DATA_TABLES} CASCADE"))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def adapter() -> AsyncIterator[BinanceAdapter]:
    limiter = AdaptiveRateLimiter(1000, 1000, 60)
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        yield BinanceAdapter(limiter, client, retry_base_sec=0)


@pytest.fixture
async def api(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    async def test_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def no_redis(*_: Any) -> bool:
        return False

    redis = FakeRedis()
    monkeypatch.setattr(market_service, "request_pair", no_redis)
    monkeypatch.setattr(market_service, "get_redis", lambda: redis)
    app.dependency_overrides[get_session] = test_session
    yield
    app.dependency_overrides.clear()


async def count(session_factory: async_sessionmaker[AsyncSession], model: Any) -> int:
    async with session_factory() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def collect_both_sides(
    adapter: BinanceAdapter, session_factory: async_sessionmaker[AsyncSession]
) -> MarketStatsTracker:
    tracker = MarketStatsTracker()
    await collect_task(adapter, BUY, tracker, session_factory)
    await collect_task(adapter, SELL, tracker, session_factory)
    return tracker


@respx.mock
async def test_collected_offers_reach_database(
    adapter: BinanceAdapter, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    respx.post(SEARCH_URL).mock(side_effect=search_responses)

    written = await collect_task(adapter, BUY, MarketStatsTracker(), session_factory)

    assert written == 20
    assert await count(session_factory, OfferSnapshot) == 20
    async with session_factory() as session:
        row = (
            await session.execute(select(OfferSnapshot).order_by(OfferSnapshot.price).limit(1))
        ).scalar_one()
        merchant = await session.get(Merchant, row.merchant_id)
        exchange = (
            await session.execute(select(Exchange).where(Exchange.code == "binance"))
        ).scalar_one()
    assert row.price == Decimal("46.10")
    assert row.side == "BUY"
    assert row.payment_methods == ["abank", "monobank", "privatbank"]
    assert row.max_amount == Decimal(528)
    assert merchant is not None and merchant.nickname == "ExTurk"
    assert exchange.last_ok_at is not None


@respx.mock
async def test_market_stats_appear_once_both_sides_are_known(
    adapter: BinanceAdapter, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    respx.post(SEARCH_URL).mock(side_effect=search_responses)
    tracker = MarketStatsTracker()

    await collect_task(adapter, BUY, tracker, session_factory)
    assert await count(session_factory, MarketStats) == 0

    await collect_task(adapter, SELL, tracker, session_factory)

    async with session_factory() as session:
        stats = (await session.execute(select(MarketStats))).scalar_one()
    assert stats.buy_price is not None and stats.sell_price is not None
    assert stats.spread is not None
    assert (stats.offers_count_buy, stats.offers_count_sell) == (20, 20)
    assert stats.volume > 0


@respx.mock
async def test_repeated_collection_adds_snapshots_but_not_merchants(
    adapter: BinanceAdapter, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    respx.post(SEARCH_URL).mock(side_effect=search_responses)
    tracker = MarketStatsTracker()

    await collect_task(adapter, BUY, tracker, session_factory)
    merchants = await count(session_factory, Merchant)
    await collect_task(adapter, BUY, tracker, session_factory)

    assert await count(session_factory, OfferSnapshot) == 40
    assert await count(session_factory, Merchant) == merchants


async def test_unchanged_merchants_do_not_consume_sequence(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    offers = [make_offer(merchant_id=f"m{i}") for i in range(5)]
    async with session_factory() as session, session.begin():
        exchange_id = await get_exchange_id(session, "binance")
        first = await upsert_merchants(session, exchange_id, offers)
        before = (await session.execute(text("SELECT last_value FROM merchants_id_seq"))).scalar()
        second = await upsert_merchants(session, exchange_id, offers)
        after = (await session.execute(text("SELECT last_value FROM merchants_id_seq"))).scalar()

    assert first == second
    assert before == after


async def test_changed_merchant_is_updated(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        exchange_id = await get_exchange_id(session, "binance")
        ids = await upsert_merchants(session, exchange_id, [make_offer(orders=10)])
        await upsert_merchants(session, exchange_id, [make_offer(orders=25)])

    async with session_factory() as session:
        merchant = await session.get(Merchant, ids["m1"])
    assert merchant is not None and merchant.orders_30d == 25


@respx.mock
async def test_latest_batch_replaces_previous_one(
    adapter: BinanceAdapter, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    route = respx.post(SEARCH_URL).mock(side_effect=search_responses)
    tracker = MarketStatsTracker()
    await collect_task(adapter, BUY, tracker, session_factory)

    body = fixture("binance_buy.json")
    body["data"] = body["data"][:3]
    route.mock(return_value=httpx.Response(200, json=body))
    await collect_task(adapter, BUY, tracker, session_factory)

    async with session_factory() as session:
        (market,) = await find_markets(session, "binance", "USDT", "UAH")
        offers = await latest_offers(session, market, "BUY")
    assert len(offers) == 3


@respx.mock
async def test_side_that_became_empty_shows_no_old_offers(
    adapter: BinanceAdapter, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    route = respx.post(SEARCH_URL).mock(side_effect=search_responses)
    tracker = MarketStatsTracker()
    await collect_task(adapter, BUY, tracker, session_factory)

    empty = {"code": "000000", "data": [], "total": 0, "success": True}
    route.mock(return_value=httpx.Response(200, json=empty))
    await collect_task(adapter, BUY, tracker, session_factory)

    async with session_factory() as session:
        (market,) = await find_markets(session, "binance", "USDT", "UAH")
        assert await latest_offers(session, market, "BUY") == []
    assert market.buy_fetched_at is not None


@respx.mock
async def test_failed_collection_writes_nothing(
    adapter: BinanceAdapter, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    respx.post(SEARCH_URL).respond(status_code=429)

    with pytest.raises(ExchangeBlocked):
        await collect_task(adapter, BUY, MarketStatsTracker(), session_factory)

    assert await count(session_factory, OfferSnapshot) == 0
    assert await count(session_factory, ExchangeMarket) == 0


async def test_failed_transaction_keeps_pointer_on_previous_batch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    broken = replace(BUY, exchange="not-in-db")

    class FakeAdapter:
        async def fetch_offers(self, *_: Any) -> list[Any]:
            return [make_offer()]

    with pytest.raises(Exception, match="No row was found"):
        await collect_task(
            FakeAdapter(),  # type: ignore[arg-type]
            broken,
            MarketStatsTracker(),
            session_factory,
        )

    assert await count(session_factory, OfferSnapshot) == 0


@respx.mock
async def test_payments_from_offers_extend_directory(
    adapter: BinanceAdapter, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    respx.post(BASE_URL + FIAT_LIST_PATH).respond(json=fixture("binance_fiat_list.json"))
    config = fixture("binance_portal_config.json")
    empty = {"code": "000000", "data": {"fiat": None, "areas": []}, "success": True}
    respx.post(BASE_URL + CONFIG_PATH).mock(
        side_effect=lambda request: httpx.Response(
            200, json=config if json.loads(request.content)["fiat"] == "UAH" else empty
        )
    )
    respx.post(SEARCH_URL).mock(side_effect=search_responses)

    pairs = await refresh_markets(adapter, session_factory)
    await collect_task(adapter, BUY, MarketStatsTracker(), session_factory)

    assert pairs >= 5
    async with session_factory() as session:
        markets = await find_markets(session, "binance", fiat="UAH")
        (usdt,) = await find_markets(session, "binance", "USDT", "UAH")
        listed_at = await markets_listed_at(session, "binance")
        directory = (await session.execute(select(PaymentMethod.code))).scalars().all()
    assert len(markets) == pairs
    assert {"monobank", "privatbank", "abank", "pumb"} <= set(usdt.payment_methods)
    assert len(usdt.payment_methods) > 4
    assert usdt.payment_methods == sorted(set(usdt.payment_methods))
    assert listed_at is not None
    assert "pumb" in directory


@respx.mock
async def test_refresh_keeps_collection_freshness(
    adapter: BinanceAdapter, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    respx.post(SEARCH_URL).mock(side_effect=search_responses)
    await collect_task(adapter, BUY, MarketStatsTracker(), session_factory)

    respx.post(BASE_URL + FIAT_LIST_PATH).respond(json=fixture("binance_fiat_list.json"))
    respx.post(BASE_URL + CONFIG_PATH).respond(json=fixture("binance_portal_config.json"))
    await refresh_markets(adapter, session_factory)

    async with session_factory() as session:
        (usdt,) = await find_markets(session, "binance", "USDT", "UAH")
    assert usdt.buy_fetched_at is not None


async def test_new_pairs_are_created_dynamically(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        first = await get_or_create_pair(session, "DOGE", "XOF")
        second = await get_or_create_pair(session, "DOGE", "XOF")
        pair = await session.get(CurrencyPair, first)

    assert first == second
    assert pair is not None


async def test_empty_offers_are_not_saved(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        assert await save_offers(session, 1, 1, []) == 0


async def test_tracked_pairs_without_users_is_empty(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        assert await tracked_pairs(session) == set()


@respx.mock
async def test_history_averages_stats_into_buckets(
    adapter: BinanceAdapter, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    respx.post(SEARCH_URL).mock(side_effect=search_responses)
    await collect_both_sides(adapter, session_factory)

    async with session_factory() as session:
        (market,) = await find_markets(session, "binance", "USDT", "UAH")
        stats_time = (await session.execute(select(func.max(MarketStats.time)))).scalar_one()
        points = await market_history(
            session,
            market.exchange_id,
            market.pair_id,
            stats_time - timedelta(hours=1),
            stats_time + timedelta(hours=1),
            timedelta(minutes=1),
        )

    assert len(points) == 1
    assert points[0].samples == 1
    assert points[0].buy_price is not None


@respx.mock
async def test_overview_and_table_stats(
    adapter: BinanceAdapter, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    respx.post(SEARCH_URL).mock(side_effect=search_responses)
    await collect_both_sides(adapter, session_factory)

    async with session_factory() as session:
        (overview,) = await exchange_overview(session, ["binance"])
        stats = {item.table: item for item in await table_stats(session)}

    assert overview.status == "active"
    assert (overview.pairs_total, overview.pairs_collected) == (1, 1)
    assert stats["offer_snapshots"].rows == 40
    assert stats["offer_snapshots"].exact is True
    assert stats["market_stats"].rows == 1


@respx.mock
async def test_api_serves_collected_data_end_to_end(
    adapter: BinanceAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    api: None,
    client: httpx.AsyncClient,
) -> None:
    respx.post(SEARCH_URL).mock(side_effect=search_responses)
    await collect_both_sides(adapter, session_factory)
    requests_to_exchange = respx.calls.call_count

    offers = await client.get(
        "/api/v1/offers",
        params={"asset": "USDT", "fiat": "UAH", "side": "BUY", "payment": "Mono", "amount": 2000},
    )
    snapshot = await client.get("/api/v1/market/snapshot", params={"asset": "USDT", "fiat": "UAH"})
    markets = await client.get("/api/v1/markets", params={"fiat": "UAH"})
    history = await client.get("/api/v1/market/history", params={"asset": "USDT", "fiat": "UAH"})

    assert offers.status_code == 200
    body = offers.json()
    assert body["count"] > 0
    assert body["freshness"][0]["stale"] is False
    for item in body["items"]:
        assert "monobank" in item["payment_methods"]
        assert Decimal(item["min_amount"]) <= 2000 <= Decimal(item["max_amount"])
        assert "E" not in item["price"]
    assert body["items"] == sorted(body["items"], key=lambda item: Decimal(item["price"]))

    assert snapshot.status_code == 200
    overall = snapshot.json()["items"][0]
    assert overall["payment_method"] is None
    assert overall["buy_price"] is not None
    assert overall["spread_pct"] is not None

    assert markets.json()["count"] == 1
    assert history.json()["count"] == 1
    assert respx.calls.call_count == requests_to_exchange
