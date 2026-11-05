import json
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.collectors.base import ExchangeError
from app.collectors.binance import AD_LIST_PATH, BASE_URL, BinanceAdapter

FIXTURE = Path(__file__).parent.parent / "fixtures" / "binance_p2p.json"
AD_LIST_URL = BASE_URL + AD_LIST_PATH


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
async def adapter() -> AsyncIterator[BinanceAdapter]:
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        yield BinanceAdapter(client)


@respx.mock
async def test_fetch_offers_normalizes_response(adapter: BinanceAdapter) -> None:
    route = respx.get(AD_LIST_URL).respond(json=_fixture())

    offers = await adapter.fetch_offers("USDT", "UAH", "BUY")

    params = route.calls.last.request.url.params
    assert (params["asset"], params["fiat"], params["tradeType"]) == ("USDT", "UAH", "BUY")
    assert len(offers) == 2
    first, second = offers
    assert first.exchange == "binance"
    assert first.side == "BUY"
    assert (first.asset, first.fiat) == ("USDT", "UAH")
    assert first.price == Decimal("46.04")
    assert first.min_amount == Decimal("500.00")
    assert first.max_amount == Decimal("2000.00")
    assert first.available == Decimal("19.98")
    assert first.external_id == "12912633653718458368"
    assert first.merchant.nickname == "Derrick Bobbs MTZ4"
    assert first.merchant.orders_30d == 2300
    assert second.merchant.completion_rate == Decimal("0.97")
    assert second.payment_methods == ["monobank", "bankvlasnyirakhunok", "abank"]
    assert first.fetched_at.tzinfo is not None


@respx.mock
async def test_fetch_offers_filters_by_payment(adapter: BinanceAdapter) -> None:
    respx.get(AD_LIST_URL).respond(json=_fixture())

    offers = await adapter.fetch_offers("USDT", "UAH", "BUY", payment="Mono")

    assert [o.merchant.nickname for o in offers] == ["Best Deal Ever"]


@respx.mock
async def test_fetch_offers_raises_on_exchange_error_code(adapter: BinanceAdapter) -> None:
    respx.get(AD_LIST_URL).respond(json={"code": "100001", "message": "bad", "data": None})

    with pytest.raises(ExchangeError):
        await adapter.fetch_offers("USDT", "UAH", "BUY")


@respx.mock
async def test_fetch_offers_raises_on_http_error(adapter: BinanceAdapter) -> None:
    respx.get(AD_LIST_URL).respond(status_code=429)

    with pytest.raises(ExchangeError):
        await adapter.fetch_offers("USDT", "UAH", "BUY")


@respx.mock
async def test_offers_endpoint(client: httpx.AsyncClient) -> None:
    respx.get(AD_LIST_URL).respond(json=_fixture())

    response = await client.get(
        "/api/v1/offers", params={"asset": "usdt", "fiat": "uah", "side": "BUY", "amount": 1500}
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["asset"], body["fiat"], body["count"]) == ("USDT", "UAH", 2)
    assert body["items"][0]["price"] == "46.04"


async def test_offers_endpoint_unknown_exchange(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/api/v1/offers",
        params={"asset": "USDT", "fiat": "UAH", "side": "BUY", "exchange": "nope"},
    )

    assert response.status_code == 404
