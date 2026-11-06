from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient

from app.db.repositories.market import MarketRow
from app.db.session import get_session
from app.domain import Offer, Side
from app.main import app
from app.services import opportunity_service
from tests.factories import make_offer

NOW = datetime.now(UTC)


def market(exchange: str, exchange_id: int, fiat: str = "UAH") -> MarketRow:
    return MarketRow(
        exchange_id=exchange_id,
        exchange=exchange,
        pair_id=7,
        asset="USDT",
        fiat=fiat,
        payment_methods=["monobank"],
        buy_fetched_at=NOW,
        sell_fetched_at=NOW,
    )


def offer(exchange: str, side: Side, price: str, **kwargs: Any) -> Offer:
    limits = {"min_amount": "100", "max_amount": "1000000", "available": "5000"}
    built = make_offer(price, side, **{**limits, **kwargs})
    return built.model_copy(update={"exchange": exchange, "fetched_at": NOW})


MARKET_OFFERS = [
    offer("binance", "BUY", "41.00"),
    offer("binance", "SELL", "40.50"),
    offer("bybit", "BUY", "42.50"),
    offer("bybit", "SELL", "41.80"),
] * 3


class FakeRepositories:
    def __init__(self) -> None:
        self.markets = [market("binance", 1), market("bybit", 2)]
        self.offers = list(MARKET_OFFERS)

    async def find_markets(self, _: Any, *args: Any, **kwargs: Any) -> list[MarketRow]:
        return self.markets

    async def fresh_offers(self, _: Any, markets: list[MarketRow], __: datetime) -> list[Offer]:
        names = {item.exchange for item in markets}
        return [item for item in self.offers if item.exchange in names]

    async def pair_risks(self, *_: Any) -> dict:
        return {}


@pytest.fixture
async def repos(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeRepositories]:
    fake = FakeRepositories()
    monkeypatch.setattr(opportunity_service, "find_markets", fake.find_markets)
    monkeypatch.setattr(opportunity_service, "fresh_offers", fake.fresh_offers)
    monkeypatch.setattr(opportunity_service, "pair_risks", fake.pair_risks)
    app.dependency_overrides[get_session] = lambda: None
    yield fake
    app.dependency_overrides.clear()


PARAMS = {"asset": "usdt", "fiat": "uah", "investment": 20000}


async def test_opportunities_after_fees(client: AsyncClient, repos: FakeRepositories) -> None:
    response = await client.get("/api/v1/opportunities", params=PARAMS)

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    item = body["items"][0]
    assert item["kind"] == "cross_exchange"
    assert (item["buy_exchange"], item["sell_exchange"]) == ("binance", "bybit")
    # 1.5 USDT за вивід з Binance
    assert Decimal(item["estimated_profit"]) == pytest.approx(
        Decimal("327.54"), abs=Decimal("0.01")
    )
    assert item["ai_score"] is not None
    assert item["ai_category"] in {
        "Excellent Opportunity",
        "Good Opportunity",
        "Medium Opportunity",
        "High Risk",
        "Not Recommended",
    }
    assert [step["action"] for step in item["steps"]] == ["BUY", "TRANSFER", "SELL"]


@pytest.mark.parametrize(
    ("param", "value"),
    [
        ("min_spread", "5"),
        ("min_net_spread", "3"),
        ("min_ai_score", "100"),
        ("min_rr", "50"),
        ("min_volume", "999999"),
        ("kind", "spread"),
        ("buy_exchange", "bybit"),
        ("sell_exchange", "binance"),
    ],
)
async def test_filters_hide_opportunity(
    client: AsyncClient, repos: FakeRepositories, param: str, value: str
) -> None:
    response = await client.get("/api/v1/opportunities", params={**PARAMS, param: value})

    assert response.status_code == 200
    assert response.json()["count"] == 0


async def test_unknown_pair_is_404(client: AsyncClient, repos: FakeRepositories) -> None:
    repos.markets = []

    response = await client.get("/api/v1/opportunities", params=PARAMS)

    assert response.status_code == 404


async def test_scanner_without_pair_filter_works(
    client: AsyncClient, repos: FakeRepositories
) -> None:
    repos.offers = [
        offer("binance", "BUY", "41.00", min_amount="5000"),
        offer("bybit", "SELL", "41.80", min_amount="5000"),
    ] * 3

    response = await client.get("/api/v1/opportunities")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert Decimal(body["items"][0]["investment"]) == 5000


async def test_typical_small_deal_can_be_eaten_by_withdrawal_fee(
    client: AsyncClient, repos: FakeRepositories
) -> None:
    # Типова сума 100 UAH — це 2.4 USDT, а вивід коштує 1.5 USDT
    response = await client.get("/api/v1/opportunities")

    assert response.json()["count"] == 0


async def test_unknown_exchange_is_404(client: AsyncClient, repos: FakeRepositories) -> None:
    response = await client.get("/api/v1/opportunities", params={"buy_exchange": "kraken"})

    assert response.status_code == 404


async def test_chain_requires_fiat(client: AsyncClient, repos: FakeRepositories) -> None:
    response = await client.get("/api/v1/opportunities", params={"kind": "chain"})

    assert response.status_code == 422
    assert "fiat" in response.json()["detail"]


async def test_chain_search_returns_only_chains(
    client: AsyncClient, repos: FakeRepositories
) -> None:
    response = await client.get(
        "/api/v1/opportunities", params={"kind": "chain", "fiat": "UAH", "investment": 20000}
    )

    assert response.status_code == 200
    assert response.json()["count"] == 0


CONTROL = {
    "asset": "USDT",
    "fiat": "UAH",
    "investment": "20000",
    "buy_price": "41.00",
    "sell_price": "41.80",
    "extra_fees": "90",
}


async def test_calculator_control_example(client: AsyncClient, repos: FakeRepositories) -> None:
    """Investment 20 000 UAH, 41.00 / 41.80, fees 90 → Net Profit 300, ROI 1.5 %."""
    response = await client.post("/api/v1/calculator", json=CONTROL)

    assert response.status_code == 200
    body = response.json()
    deal = body["deal"]
    assert round(Decimal(deal["quantity"]), 2) == Decimal("487.80")
    assert round(Decimal(deal["net_profit"])) == 300
    assert round(Decimal(deal["roi_pct"]), 1) == Decimal("1.5")
    assert body["buy_slippage_pct"] is None
    assert body["fully_filled"] is True
    # 90 + 0.5 % від 20 000 = 190; RR = 300.24 / 190
    assert Decimal(body["potential_loss"]) == 190
    assert body["rr"] == "1.5802"


async def test_calculator_takes_prices_from_market(
    client: AsyncClient, repos: FakeRepositories
) -> None:
    response = await client.post(
        "/api/v1/calculator",
        json={
            "asset": "USDT",
            "fiat": "UAH",
            "investment": "20000",
            "buy_exchange": "binance",
            "sell_exchange": "bybit",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert Decimal(body["deal"]["buy_price"]) == Decimal("41.00")
    assert Decimal(body["deal"]["sell_price"]) == Decimal("41.80")
    assert Decimal(body["buy_slippage_pct"]) == 0
    assert body["fully_filled"] is True
    # Вивід між біржами враховано: 1.5 USDT з Binance
    assert Decimal(body["deal"]["sell_quantity"]) < Decimal(body["deal"]["quantity"])


async def test_calculator_reports_slippage_and_shortage(
    client: AsyncClient, repos: FakeRepositories
) -> None:
    repos.offers = [
        offer("binance", "BUY", "41.00", available="100"),
        offer("binance", "BUY", "41.50", available="100"),
        offer("bybit", "SELL", "41.80", available="5000"),
    ]

    response = await client.post(
        "/api/v1/calculator",
        json={
            "asset": "USDT",
            "fiat": "UAH",
            "investment": "20000",
            "buy_exchange": "binance",
            "sell_exchange": "bybit",
        },
    )

    body = response.json()
    assert Decimal(body["buy_slippage_pct"]) > 0
    assert body["fully_filled"] is False


async def test_calculator_without_offers_is_409(
    client: AsyncClient, repos: FakeRepositories
) -> None:
    repos.offers = []

    response = await client.post(
        "/api/v1/calculator",
        json={
            "asset": "USDT",
            "fiat": "UAH",
            "investment": "20000",
            "buy_exchange": "binance",
            "sell_exchange": "bybit",
        },
    )

    assert response.status_code == 409
    assert "оголошень" in response.json()["detail"]


async def test_calculator_requires_price_source(
    client: AsyncClient, repos: FakeRepositories
) -> None:
    response = await client.post(
        "/api/v1/calculator", json={"asset": "USDT", "fiat": "UAH", "investment": "20000"}
    )

    assert response.status_code == 422


async def test_calculator_rejects_non_positive_investment(
    client: AsyncClient, repos: FakeRepositories
) -> None:
    response = await client.post("/api/v1/calculator", json={**CONTROL, "investment": "0"})

    assert response.status_code == 422
