from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient

from app.db.repositories.market import (
    ExchangeOverview,
    HistoryPoint,
    MarketRow,
    TableStats,
)
from app.db.session import get_session
from app.domain import CollectorStatus, Offer, Side
from app.main import app
from app.services import market_service
from tests.factories import make_offer

NOW = datetime.now(UTC)
MARKET = MarketRow(
    exchange_id=1,
    exchange="binance",
    pair_id=7,
    asset="USDT",
    fiat="UAH",
    payment_methods=["monobank", "privatbank"],
    buy_fetched_at=NOW,
    sell_fetched_at=NOW,
)
STALE_MARKET = replace(
    MARKET,
    buy_fetched_at=NOW - timedelta(hours=1),
    sell_fetched_at=NOW - timedelta(hours=1),
)


class FakeServices:
    """Заміна репозиторіїв: API-шар перевіряється без БД і Redis."""

    def __init__(self) -> None:
        self.markets = [MARKET]
        self.offers: dict[Side, list[Offer]] = {
            "BUY": [
                make_offer("41.30", merchant_id="b"),
                make_offer("41.20", merchant_id="a", payments=("privatbank",)),
                make_offer("41.25", merchant_id="c", min_amount="10000"),
            ],
            "SELL": [make_offer("41.00", "SELL"), make_offer("41.10", "SELL")],
        }
        self.requested: list[tuple[str, str]] = []
        self.find_calls: list[tuple[str | None, ...]] = []

    async def find_markets(self, _: Any, *args: str | None) -> list[MarketRow]:
        self.find_calls.append(args)
        return self.markets

    async def latest_offers(self, _: Any, __: MarketRow, side: Side) -> list[Offer]:
        return self.offers[side]

    async def request_pair(self, _: Any, asset: str, fiat: str) -> bool:
        self.requested.append((asset, fiat))
        return True


@pytest.fixture
async def services(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeServices]:
    fake = FakeServices()
    monkeypatch.setattr(market_service, "find_markets", fake.find_markets)
    monkeypatch.setattr(market_service, "latest_offers", fake.latest_offers)
    monkeypatch.setattr(market_service, "request_pair", fake.request_pair)
    monkeypatch.setattr("app.api.v1.market.find_markets", fake.find_markets)
    app.dependency_overrides[get_session] = lambda: None
    yield fake
    app.dependency_overrides.clear()


OFFERS_PARAMS = {"asset": "usdt", "fiat": "uah", "side": "BUY"}


async def test_offers_are_sorted_from_best_for_buyer(
    client: AsyncClient, services: FakeServices
) -> None:
    response = await client.get("/api/v1/offers", params=OFFERS_PARAMS)

    assert response.status_code == 200
    body = response.json()
    assert (body["asset"], body["fiat"], body["count"]) == ("USDT", "UAH", 3)
    assert [item["price"] for item in body["items"]] == ["41.20", "41.25", "41.30"]


async def test_offers_are_sorted_from_best_for_seller(
    client: AsyncClient, services: FakeServices
) -> None:
    response = await client.get("/api/v1/offers", params={**OFFERS_PARAMS, "side": "SELL"})

    assert [item["price"] for item in response.json()["items"]] == ["41.10", "41.00"]


async def test_offers_filter_by_normalized_payment(
    client: AsyncClient, services: FakeServices
) -> None:
    response = await client.get("/api/v1/offers", params={**OFFERS_PARAMS, "payment": "Privat24"})

    assert [item["price"] for item in response.json()["items"]] == ["41.20"]


async def test_offers_filter_by_amount_in_fiat(client: AsyncClient, services: FakeServices) -> None:
    response = await client.get("/api/v1/offers", params={**OFFERS_PARAMS, "amount": 15000})

    assert response.json()["count"] == 3
    response = await client.get("/api/v1/offers", params={**OFFERS_PARAMS, "amount": 1000})
    assert [item["price"] for item in response.json()["items"]] == ["41.20", "41.30"]


async def test_fresh_data_does_not_bother_collector(
    client: AsyncClient, services: FakeServices
) -> None:
    body = (await client.get("/api/v1/offers", params=OFFERS_PARAMS)).json()

    assert body["queued"] is False
    assert body["freshness"][0]["stale"] is False
    assert services.requested == []


async def test_stale_data_raises_pair_priority(client: AsyncClient, services: FakeServices) -> None:
    services.markets = [STALE_MARKET]

    body = (await client.get("/api/v1/offers", params=OFFERS_PARAMS)).json()

    assert body["queued"] is True
    assert body["freshness"][0]["stale"] is True
    assert body["freshness"][0]["age_sec"] > 3000
    assert services.requested == [("USDT", "UAH")]


async def test_pair_without_data_is_queued(client: AsyncClient, services: FakeServices) -> None:
    services.markets = [replace(MARKET, buy_fetched_at=None, sell_fetched_at=None)]
    services.offers = {"BUY": [], "SELL": []}

    body = (await client.get("/api/v1/offers", params=OFFERS_PARAMS)).json()

    assert body["count"] == 0
    assert body["queued"] is True
    assert body["freshness"][0]["age_sec"] is None


async def test_unknown_pair_is_404_and_not_queued(
    client: AsyncClient, services: FakeServices
) -> None:
    services.markets = []

    response = await client.get("/api/v1/offers", params=OFFERS_PARAMS)

    assert response.status_code == 404
    assert "USDT/UAH" in response.json()["detail"]
    assert services.requested == []


async def test_offers_unknown_exchange(client: AsyncClient, services: FakeServices) -> None:
    response = await client.get("/api/v1/offers", params={**OFFERS_PARAMS, "exchange": "nope"})

    assert response.status_code == 404


@pytest.mark.parametrize("params", [{"asset": "US DT"}, {"fiat": "UAH;DROP"}, {"side": "HOLD"}])
async def test_offers_rejects_invalid_params(
    client: AsyncClient, services: FakeServices, params: dict[str, str]
) -> None:
    response = await client.get("/api/v1/offers", params={**OFFERS_PARAMS, **params})

    assert response.status_code == 422


async def test_offers_rejects_non_positive_amount(
    client: AsyncClient, services: FakeServices
) -> None:
    response = await client.get("/api/v1/offers", params={**OFFERS_PARAMS, "amount": 0})

    assert response.status_code == 422


async def test_markets_lists_pairs_with_freshness(
    client: AsyncClient, services: FakeServices
) -> None:
    response = await client.get("/api/v1/markets", params={"asset": "usdt"})

    assert response.status_code == 200
    body = response.json()
    assert (body["count"], body["assets"], body["fiats"]) == (1, ["USDT"], ["UAH"])
    assert body["items"][0]["payment_methods"] == ["monobank", "privatbank"]
    assert body["items"][0]["buy_fetched_at"] is not None
    assert services.find_calls == [(None, "USDT", None)]


async def test_snapshot_returns_overall_and_per_payment_prices(
    client: AsyncClient, services: FakeServices
) -> None:
    response = await client.get("/api/v1/market/snapshot", params={"asset": "USDT", "fiat": "UAH"})

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["payment_method"] for item in items] == [None, "monobank", "privatbank"]
    overall, mono, privat = items
    assert overall["buy_price"] == "41.25"
    assert overall["sell_price"] == "41.05"
    assert overall["offers_count_buy"] == 3
    assert mono["offers_count_buy"] == 2
    assert privat["offers_count_sell"] == 0
    assert privat["sell_price"] is None


async def test_snapshot_amount_drops_offers_outside_limits(
    client: AsyncClient, services: FakeServices
) -> None:
    services.offers["SELL"] = [
        make_offer("49.00", "SELL", min_amount="350000", max_amount="1000000"),
        make_offer("47.00", "SELL", min_amount="1000", max_amount="50000"),
    ]

    response = await client.get(
        "/api/v1/market/snapshot", params={"asset": "USDT", "fiat": "UAH", "amount": 5000}
    )

    overall = response.json()["items"][0]
    assert overall["sell_price"] == "47.00"
    assert overall["offers_count_sell"] == 1


async def test_snapshot_skips_exchange_without_data(
    client: AsyncClient, services: FakeServices
) -> None:
    services.markets = [replace(MARKET, buy_fetched_at=None, sell_fetched_at=None)]

    response = await client.get("/api/v1/market/snapshot", params={"asset": "USDT", "fiat": "UAH"})

    assert response.json()["items"] == []
    assert response.json()["queued"] is True


async def test_history_returns_rounded_points(
    client: AsyncClient, services: FakeServices, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def fake_history(_: Any, *args: Any) -> list[HistoryPoint]:
        captured["args"] = args
        point = HistoryPoint(
            NOW, Decimal("41.123456"), Decimal("41.5"), Decimal("0.91"), Decimal("1200.5"), 12
        )
        return [point]

    monkeypatch.setattr(market_service, "market_history", fake_history)

    response = await client.get(
        "/api/v1/market/history", params={"asset": "USDT", "fiat": "UAH", "interval": "5m"}
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["exchange"], body["interval"], body["count"]) == ("binance", "5m", 1)
    assert body["items"][0]["buy_price"] == "41.1235"
    assert body["items"][0]["samples"] == 12
    exchange_id, pair_id, start, end, interval = captured["args"]
    assert (exchange_id, pair_id, interval) == (1, 7, timedelta(minutes=5))
    assert end - start == timedelta(hours=1)


async def test_history_rejects_reversed_period(client: AsyncClient, services: FakeServices) -> None:
    params = {
        "asset": "USDT",
        "fiat": "UAH",
        "from": "2026-11-06T12:00:00Z",
        "to": "2026-11-06T11:00:00Z",
    }

    response = await client.get("/api/v1/market/history", params=params)

    assert response.status_code == 422


async def test_history_rejects_too_many_points(client: AsyncClient, services: FakeServices) -> None:
    params = {
        "asset": "USDT",
        "fiat": "UAH",
        "from": "2026-10-01T00:00:00Z",
        "to": "2026-11-01T00:00:00Z",
        "interval": "1m",
    }

    response = await client.get("/api/v1/market/history", params=params)

    assert response.status_code == 422
    assert "interval" in response.json()["detail"]


async def test_collector_status_reports_db_and_collector(
    client: AsyncClient, services: FakeServices, monkeypatch: pytest.MonkeyPatch
) -> None:
    status = CollectorStatus(
        exchange="binance",
        rps=3.5,
        rps_ceiling=10,
        limiter_state="ok",
        paused_for_sec=0,
        tasks={"high": 2, "popular": 10, "rest": 100},
        requests_ok=500,
        requests_failed=1,
        rows_written=9000,
        started_at=NOW,
        updated_at=NOW,
    )

    async def fake_overview(_: Any, __: Any) -> list[ExchangeOverview]:
        return [ExchangeOverview("binance", "active", NOW, 800, 120)]

    async def fake_read_status(_: Any, __: str) -> CollectorStatus:
        return status

    async def fake_tables(_: Any) -> list[TableStats]:
        return [TableStats("offer_snapshots", 9000, True, 3.5)]

    async def fake_merchants(_: Any) -> int:
        return 42

    monkeypatch.setattr(market_service, "exchange_overview", fake_overview)
    monkeypatch.setattr(market_service, "read_status", fake_read_status)
    monkeypatch.setattr(market_service, "table_stats", fake_tables)
    monkeypatch.setattr(market_service, "merchants_count", fake_merchants)

    response = await client.get("/api/v1/collector/status")

    assert response.status_code == 200
    body = response.json()
    assert body["merchants"] == 42
    assert body["tables"][0]["rows"] == 9000
    exchange = body["exchanges"][0]
    assert exchange["collector_running"] is True
    assert exchange["collector"]["rps"] == 3.5
    assert (exchange["pairs_total"], exchange["pairs_collected"]) == (800, 120)


async def test_collector_status_shows_stopped_collector(
    client: AsyncClient, services: FakeServices, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_overview(_: Any, __: Any) -> list[ExchangeOverview]:
        return [ExchangeOverview("binance", "active", None, 0, 0)]

    async def no_status(_: Any, __: str) -> None:
        return None

    async def no_tables(_: Any) -> list[TableStats]:
        return []

    async def no_merchants(_: Any) -> int:
        return 0

    monkeypatch.setattr(market_service, "exchange_overview", fake_overview)
    monkeypatch.setattr(market_service, "read_status", no_status)
    monkeypatch.setattr(market_service, "table_stats", no_tables)
    monkeypatch.setattr(market_service, "merchants_count", no_merchants)

    body = (await client.get("/api/v1/collector/status")).json()

    assert body["exchanges"][0]["collector_running"] is False
    assert body["exchanges"][0]["collector"] is None


async def test_database_outage_returns_503(
    client: AsyncClient, services: FakeServices, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_: Any) -> list[MarketRow]:
        raise ConnectionRefusedError("db is down")

    monkeypatch.setattr(market_service, "find_markets", broken)

    response = await client.get("/api/v1/offers", params=OFFERS_PARAMS)

    assert response.status_code == 503
    assert response.json()["detail"] == "База даних тимчасово недоступна"
