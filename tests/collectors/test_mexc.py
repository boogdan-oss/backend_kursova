from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
import respx

from app.collectors.base import ExchangeBlocked, ExchangeError
from app.collectors.mexc import BASE_URL, COINS_PATH, CURRENCIES_PATH, MARKET_PATH, MexcAdapter
from tests.collectors.exchange_cases import fixture, make_adapter

MARKET_URL = BASE_URL + MARKET_PATH
USDT_ID = "128f589271cb4951b03e71e6323eb7be"


@pytest.fixture
async def adapter() -> AsyncIterator[MexcAdapter]:
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        yield make_adapter(MexcAdapter, client)


def mock_coins() -> respx.Route:
    return respx.get(BASE_URL + COINS_PATH).respond(json=fixture("mexc_coins.json"))


def by_side(request: httpx.Request) -> httpx.Response:
    trade_type = request.url.params["tradeType"]
    return httpx.Response(
        200, json=fixture("mexc_buy.json" if trade_type == "SELL" else "mexc_sell.json")
    )


@respx.mock
async def test_buy_side_asks_for_sellers_by_coin_id(adapter: MexcAdapter) -> None:
    mock_coins()
    route = respx.get(MARKET_URL).mock(side_effect=by_side)

    offers = await adapter.fetch_offers("USDT", "UAH", "BUY")

    params = route.calls.last.request.url.params
    assert (params["tradeType"], params["coinId"], params["currency"]) == ("SELL", USDT_ID, "UAH")
    assert params["pageSize"] == "20"
    assert len(offers) == 20
    first = offers[0]
    assert (first.exchange, first.side, first.asset, first.fiat) == ("mexc", "BUY", "USDT", "UAH")
    assert first.price == Decimal("44.96")
    assert first.available == Decimal("323.0178")
    assert (first.min_amount, first.max_amount) == (3200, 4720)
    assert first.merchant.id == "46475533a83d4c369d5fe9119432dcbb"
    assert first.merchant.nickname == "JonhyCash"
    assert first.merchant.orders_30d == 240
    assert first.merchant.completion_rate == 1
    assert first.payment_methods == ["mexc128", "mexc130", "mexc131", "mexc146"]


@respx.mock
async def test_sell_side_asks_for_buyers(adapter: MexcAdapter) -> None:
    mock_coins()
    route = respx.get(MARKET_URL).mock(side_effect=by_side)

    offers = await adapter.fetch_offers("USDT", "UAH", "SELL")

    assert route.calls.last.request.url.params["tradeType"] == "BUY"
    assert {offer.side for offer in offers} == {"SELL"}


@respx.mock
async def test_coin_list_is_loaded_once(adapter: MexcAdapter) -> None:
    coins = mock_coins()
    respx.get(MARKET_URL).mock(side_effect=by_side)

    await adapter.fetch_offers("USDT", "UAH", "BUY")
    await adapter.fetch_offers("BTC", "UAH", "BUY")

    assert coins.call_count == 1


@respx.mock
async def test_unknown_asset_is_empty_market(adapter: MexcAdapter) -> None:
    mock_coins()
    market = respx.get(MARKET_URL).mock(side_effect=by_side)

    assert await adapter.fetch_offers("NOPE", "UAH", "BUY") == []
    assert market.call_count == 0


@respx.mock
async def test_error_code(adapter: MexcAdapter) -> None:
    mock_coins()
    respx.get(MARKET_URL).respond(json={"code": 1, "msg": "Not Found"})

    with pytest.raises(ExchangeError, match="Not Found"):
        await adapter.fetch_offers("USDT", "UAH", "BUY")


@respx.mock
async def test_rate_limit_is_a_block(adapter: MexcAdapter) -> None:
    mock_coins()
    respx.get(MARKET_URL).respond(status_code=429)

    with pytest.raises(ExchangeBlocked):
        await adapter.fetch_offers("USDT", "UAH", "BUY")


@respx.mock
async def test_list_markets_uses_active_currencies(adapter: MexcAdapter) -> None:
    mock_coins()
    respx.get(BASE_URL + CURRENCIES_PATH).respond(json=fixture("mexc_currencies.json"))

    markets = await adapter.list_markets()

    coins = {coin["coinName"] for coin in fixture("mexc_coins.json")["data"]}
    active = {c["currency"] for c in fixture("mexc_currencies.json")["data"] if c["state"] == 1}
    assert {(m.asset, m.fiat) for m in markets} == {(c, f) for c in coins for f in active}
    assert "UAH" in active
