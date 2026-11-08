"""Наскрізний тест можливостей на справжній БД: дві біржі → сканер → PostgreSQL → API.

Запуск: див. test_collect_flow.py. Таблиці очищуються, тому робочу БД сюди не підставляти.
"""

import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.collectors.binance import BASE_URL as BINANCE_URL
from app.collectors.binance import SEARCH_PATH, BinanceAdapter
from app.collectors.bybit import BASE_URL as BYBIT_URL
from app.collectors.bybit import ITEMS_PATH, PAYMENTS_PATH, BybitAdapter
from app.collectors.ratelimit import AdaptiveRateLimiter
from app.collectors.scheduler import CollectTask
from app.db.models import Merchant, Opportunity, SpreadHistory
from app.db.repositories.market import find_markets
from app.db.repositories.offers import fresh_offers
from app.db.session import get_session
from app.domain import Offer
from app.main import app
from app.services import opportunity_service
from app.services.collect_service import MarketStatsTracker, collect_task
from app.services.opportunity_service import OpportunityScanner, get_opportunity_history
from tests.factories import make_offer

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL не заданий")

FIXTURES = Path(__file__).parent.parent / "fixtures"
TABLES = "offer_snapshots, market_stats, spread_history, opportunities, exchange_markets, merchants"
SIDES = ("BUY", "SELL")


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def binance_response(request: httpx.Request) -> httpx.Response:
    side = json.loads(request.content)["tradeType"].lower()
    return httpx.Response(200, json=fixture(f"binance_{side}.json"))


def bybit_response(request: httpx.Request) -> httpx.Response:
    side = {"1": "buy", "0": "sell"}[json.loads(request.content)["side"]]
    return httpx.Response(200, json=fixture(f"bybit_{side}.json"))


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def adapters() -> AsyncIterator[tuple[BinanceAdapter, BybitAdapter]]:
    async with (
        httpx.AsyncClient(base_url=BINANCE_URL) as binance_client,
        httpx.AsyncClient(base_url=BYBIT_URL) as bybit_client,
    ):
        yield (
            BinanceAdapter(AdaptiveRateLimiter(1000, 1000, 60), binance_client, 0),
            BybitAdapter(AdaptiveRateLimiter(1000, 1000, 60), bybit_client, 0),
        )


def mock_exchanges() -> None:
    respx.post(BINANCE_URL + SEARCH_PATH).mock(side_effect=binance_response)
    respx.post(BYBIT_URL + ITEMS_PATH).mock(side_effect=bybit_response)
    respx.post(BYBIT_URL + PAYMENTS_PATH).respond(json=fixture("bybit_payments.json"))


async def collect_all(
    adapters: tuple[BinanceAdapter, BybitAdapter],
    tracker: MarketStatsTracker,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    for adapter in adapters:
        for side in SIDES:
            task = CollectTask(adapter.name, "USDT", "UAH", side)  # type: ignore[arg-type]
            await collect_task(adapter, task, tracker, session_factory)


async def count(session_factory: async_sessionmaker[AsyncSession], model: Any) -> int:
    async with session_factory() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


def profitable_offers() -> list[Offer]:
    now = datetime.now(UTC)
    limits = {"min_amount": "5000", "max_amount": "100000", "available": "5000"}
    offers = []
    for exchange, side, price in (
        ("binance", "BUY", "41.00"),
        ("binance", "SELL", "40.50"),
        ("bybit", "BUY", "42.50"),
        ("bybit", "SELL", "41.80"),
    ):
        offer = make_offer(price, side, **limits)  # type: ignore[arg-type]
        offers.append(offer.model_copy(update={"exchange": exchange, "fetched_at": now}))
    return offers * 3


class FixedTracker(MarketStatsTracker):
    """Трекер із заданими оголошеннями: сканер перевіряється на відомому результаті."""

    def __init__(self, offers: list[Offer]) -> None:
        super().__init__()
        self.offers = offers

    def offers_since(self, since: datetime) -> list[Offer]:
        return self.offers


@respx.mock
async def test_bybit_offers_reach_database_with_own_merchants(
    adapters: tuple[BinanceAdapter, BybitAdapter],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    mock_exchanges()

    await collect_all(adapters, MarketStatsTracker(), session_factory)

    async with session_factory() as session:
        markets = await find_markets(session, asset="USDT", fiat="UAH")
        offers = await fresh_offers(session, markets, datetime.now(UTC) - timedelta(minutes=5))
    assert {market.exchange for market in markets} == {"binance", "bybit"}
    assert {offer.exchange for offer in offers} == {"binance", "bybit"}
    assert len(offers) == 80
    assert {offer.side for offer in offers} == {"BUY", "SELL"}
    bybit_first = next(offer for offer in offers if offer.merchant.nickname == "ZalchikNalchikT")
    assert bybit_first.exchange == "bybit"
    assert bybit_first.payment_methods == ["abank"]
    assert await count(session_factory, Merchant) > 20


async def test_fresh_offers_skip_old_batches(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        assert await fresh_offers(session, [], datetime.now(UTC)) == []


async def prepare_pair(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """exchange_markets для обох бірж: сканер бере звідти id пар і бірж."""
    async with session_factory() as session, session.begin():
        pair_id = (
            await session.execute(
                text(
                    "SELECT p.id FROM currency_pairs p JOIN assets a ON a.id = p.base_currency_id "
                    "JOIN fiats f ON f.id = p.quote_currency_id "
                    "WHERE a.code = 'USDT' AND f.code = 'UAH'"
                )
            )
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO exchange_markets (exchange_id, pair_id, payment_methods) "
                "SELECT id, :pair_id, '{}' FROM exchanges WHERE code IN ('binance', 'bybit')"
            ),
            {"pair_id": pair_id},
        )


async def test_scanner_writes_best_direction_to_spread_history(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await prepare_pair(session_factory)

    await OpportunityScanner(FixedTracker(profitable_offers()), set(), session_factory).scan()

    async with session_factory() as session:
        row = (await session.execute(select(SpreadHistory))).scalar_one()
    assert (row.buy_price, row.sell_price) == (Decimal("41.00"), Decimal("41.80"))
    assert row.spread == Decimal("1.9512")
    assert row.net_spread > 0


@respx.mock
async def test_real_market_without_usable_offers_gives_no_opportunity(
    adapters: tuple[BinanceAdapter, BybitAdapter],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """У фікстурах дешеві оголошення — точні лоти (min = max): для типової суми ринку немає."""
    mock_exchanges()
    tracker = MarketStatsTracker()
    await collect_all(adapters, tracker, session_factory)

    await OpportunityScanner(tracker, {("USDT", "UAH")}, session_factory).scan()

    assert await count(session_factory, Opportunity) == 0


async def test_opportunity_is_one_row_and_duration_grows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await prepare_pair(session_factory)
    scanner = OpportunityScanner(FixedTracker(profitable_offers()), set(), session_factory)

    await scanner.scan()
    assert await count(session_factory, Opportunity) == 1
    assert scanner.active_pairs() == {("USDT", "UAH")}

    (key,) = scanner._open
    row_id, started_at = scanner._open[key]
    earlier = started_at - timedelta(seconds=90)
    async with session_factory() as session, session.begin():
        await session.execute(
            text("UPDATE opportunities SET time = :earlier WHERE id = :id"),
            {"earlier": earlier, "id": row_id},
        )
    scanner._open[key] = (row_id, earlier)

    await scanner.scan()

    assert await count(session_factory, Opportunity) == 1
    async with session_factory() as session:
        (record,) = await get_opportunity_history(session, "USDT", "UAH", None, 24, 10)
    assert record.duration_sec is not None and record.duration_sec >= 90
    assert record.opportunity.kind == "cross_exchange"
    assert (record.opportunity.buy_exchange, record.opportunity.sell_exchange) == (
        "binance",
        "bybit",
    )
    assert [step.action for step in record.opportunity.steps] == ["BUY", "TRANSFER", "SELL"]


async def test_closed_opportunity_reopens_as_new_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await prepare_pair(session_factory)
    tracker = FixedTracker(profitable_offers())
    scanner = OpportunityScanner(tracker, set(), session_factory)

    await scanner.scan()
    tracker.offers = [offer for offer in tracker.offers if offer.exchange == "binance"]
    await scanner.scan()
    assert scanner.active_pairs() == set()

    tracker.offers = profitable_offers()
    await scanner.scan()

    assert await count(session_factory, Opportunity) == 2


async def test_scanner_without_offers_writes_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await OpportunityScanner(FixedTracker([]), set(), session_factory).scan()

    assert await count(session_factory, Opportunity) == 0


@respx.mock
async def test_api_compares_exchanges_from_database(
    adapters: tuple[BinanceAdapter, BybitAdapter],
    session_factory: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_exchanges()
    await collect_all(adapters, MarketStatsTracker(), session_factory)
    requests_to_exchanges = respx.calls.call_count

    async def test_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = test_session
    try:
        offers = await client.get(
            "/api/v1/offers", params={"asset": "USDT", "fiat": "UAH", "side": "BUY"}
        )
        found = await client.get(
            "/api/v1/opportunities", params={"asset": "USDT", "fiat": "UAH", "investment": 20000}
        )
        calculator = await client.post(
            "/api/v1/calculator",
            json={
                "asset": "USDT",
                "fiat": "UAH",
                "investment": "20000",
                "buy_exchange": "bybit",
                "sell_exchange": "binance",
            },
        )
        history = await client.get("/api/v1/opportunities/history")
    finally:
        app.dependency_overrides.clear()

    assert {item["exchange"] for item in offers.json()["items"]} == {"binance", "bybit"}
    assert found.status_code == 200
    for item in found.json()["items"]:
        assert item["ai_score"] is not None
        assert float(item["estimated_profit"]) > 0
    assert calculator.status_code == 200
    assert calculator.json()["buy_slippage_pct"] is not None
    assert history.status_code == 200
    assert respx.calls.call_count == requests_to_exchanges
    assert opportunity_service.OFFERS_MAX_AGE == timedelta(minutes=5)
