from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
import respx

from app.collectors.base import ExchangeBlocked, ExchangeError
from app.collectors.htx import BASE_URL, CONFIG_PATH, MARKET_PATH, HtxAdapter
from tests.collectors.exchange_cases import fixture, make_adapter

MARKET_URL = BASE_URL + MARKET_PATH


@pytest.fixture
async def adapter() -> AsyncIterator[HtxAdapter]:
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        yield make_adapter(HtxAdapter, client)


def mock_config() -> respx.Route:
    return respx.get(BASE_URL + CONFIG_PATH).respond(json=fixture("htx_config.json"))


def by_side(request: httpx.Request) -> httpx.Response:
    trade_type = request.url.params["tradeType"]
    return httpx.Response(
        200, json=fixture("htx_buy.json" if trade_type == "sell" else "htx_sell.json")
    )


@respx.mock
async def test_buy_side_reads_two_htx_pages_by_ids(adapter: HtxAdapter) -> None:
    mock_config()
    route = respx.get(MARKET_URL).mock(side_effect=by_side)

    offers = await adapter.fetch_offers("USDT", "UAH", "BUY")

    pages = [call.request.url.params for call in route.calls]
    assert [(p["tradeType"], p["coinId"], p["currency"], p["currPage"]) for p in pages] == [
        ("sell", "2", "45", "1"),
        ("sell", "2", "45", "2"),
    ]
    assert len(offers) == 20
    first = offers[0]
    assert (first.exchange, first.side, first.asset, first.fiat) == ("htx", "BUY", "USDT", "UAH")
    assert first.price == Decimal("46.80")
    assert first.available == Decimal("371.729391")
    assert (first.min_amount, first.max_amount) == (15000, 17396)
    assert first.merchant.id == "381569321"
    assert first.merchant.nickname == "FastUkraine"
    assert first.merchant.orders_30d == 0
    assert first.external_id == "855499"
    assert first.payment_methods == ["privatbank"]


@respx.mock
async def test_sell_side_converts_percent_completion(adapter: HtxAdapter) -> None:
    mock_config()
    respx.get(MARKET_URL).mock(side_effect=by_side)

    offers = await adapter.fetch_offers("USDT", "UAH", "SELL")

    first = offers[0]
    assert first.side == "SELL"
    assert first.price == 52
    assert first.merchant.orders_30d == 5
    assert first.merchant.completion_rate == Decimal("0.45")
    assert first.payment_methods == ["banktransfer"]


@respx.mock
async def test_short_first_page_skips_second(adapter: HtxAdapter) -> None:
    mock_config()
    body = fixture("htx_buy.json")
    body["data"] = body["data"][:4]
    route = respx.get(MARKET_URL).respond(json=body)

    offers = await adapter.fetch_offers("USDT", "UAH", "BUY")

    assert route.call_count == 1
    assert len(offers) == 4


@respx.mock
async def test_second_page_maps_to_htx_pages_three_and_four(adapter: HtxAdapter) -> None:
    mock_config()
    route = respx.get(MARKET_URL).mock(side_effect=by_side)

    await adapter.fetch_offers("USDT", "UAH", "BUY", page=2)

    assert [call.request.url.params["currPage"] for call in route.calls] == ["3", "4"]


@respx.mock
async def test_pair_without_ids_is_empty_market(adapter: HtxAdapter) -> None:
    mock_config()
    market = respx.get(MARKET_URL).mock(side_effect=by_side)

    assert await adapter.fetch_offers("USDT", "CNY", "BUY") == []
    assert await adapter.fetch_offers("DOGE", "UAH", "BUY") == []
    assert market.call_count == 0


@respx.mock
async def test_error_code(adapter: HtxAdapter) -> None:
    mock_config()
    respx.get(MARKET_URL).respond(json={"code": 500, "message": "busy", "data": None})

    with pytest.raises(ExchangeError, match="busy"):
        await adapter.fetch_offers("USDT", "UAH", "BUY")


@respx.mock
async def test_rate_limit_is_a_block(adapter: HtxAdapter) -> None:
    mock_config()
    respx.get(MARKET_URL).respond(status_code=429)

    with pytest.raises(ExchangeBlocked):
        await adapter.fetch_offers("USDT", "UAH", "BUY")


@respx.mock
async def test_list_markets_uses_p2p_currencies(adapter: HtxAdapter) -> None:
    mock_config()

    markets = await adapter.list_markets()

    coins = {item["coinName"] for item in fixture("htx_config.json")["data"]["marketQuery"]}
    assert {(m.asset, m.fiat) for m in markets} == {(c, f) for c in coins for f in ("UAH", "EUR")}
    uah = next(m for m in markets if m.fiat == "UAH")
    assert "privatbank" in uah.payment_methods
