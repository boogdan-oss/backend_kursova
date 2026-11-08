import json
from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
import respx

from app.collectors.base import ExchangeBlocked, ExchangeError
from app.collectors.bitget import ADS_PATH, BASE_URL, CONFIG_PATH, BitgetAdapter
from tests.collectors.exchange_cases import fixture, make_adapter

ADS_URL = BASE_URL + ADS_PATH


@pytest.fixture
async def adapter() -> AsyncIterator[BitgetAdapter]:
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        yield make_adapter(BitgetAdapter, client)


def by_side(request: httpx.Request) -> httpx.Response:
    side = json.loads(request.content)["side"]
    return httpx.Response(200, json=fixture("bitget_buy.json" if side == 1 else "bitget_sell.json"))


@respx.mock
async def test_buy_side_uses_what_is_left_in_offer(adapter: BitgetAdapter) -> None:
    route = respx.post(ADS_URL).mock(side_effect=by_side)

    offers = await adapter.fetch_offers("USDT", "UAH", "BUY")

    body = json.loads(route.calls.last.request.content)
    assert (body["side"], body["coinCode"], body["fiatCode"], body["pageSize"]) == (
        1,
        "USDT",
        "UAH",
        20,
    )
    assert len(offers) == 20
    first = offers[0]
    assert (first.exchange, first.side, first.asset, first.fiat) == ("bitget", "BUY", "USDT", "UAH")
    assert first.price == 41
    assert first.available == Decimal("3.6")
    assert (first.min_amount, first.max_amount) == (45, 50)
    assert first.merchant.id == "b0b14c728fba3f55a692"
    assert first.merchant.orders_30d == 5
    assert first.merchant.completion_rate == Decimal("0.83")
    assert first.external_id == "1500891608232202241"
    assert first.payment_methods[0] == "privatbank"


@respx.mock
async def test_sell_side_limits_max_by_what_is_left(adapter: BitgetAdapter) -> None:
    respx.post(ADS_URL).mock(side_effect=by_side)

    offers = await adapter.fetch_offers("USDT", "UAH", "SELL")

    first = offers[0]
    assert first.side == "SELL"
    assert first.price == Decimal("46.75")
    assert first.max_amount == Decimal("361.09") * Decimal("46.75")
    assert first.merchant.nickname == "Valadosbarbos"
    assert "raiffeisen" in first.payment_methods
    assert [o.price for o in offers] == sorted((o.price for o in offers), reverse=True)


@respx.mock
async def test_page_is_passed(adapter: BitgetAdapter) -> None:
    route = respx.post(ADS_URL).mock(side_effect=by_side)

    await adapter.fetch_offers("USDT", "UAH", "BUY", page=3)

    assert json.loads(route.calls.last.request.content)["pageNo"] == 3


@respx.mock
async def test_empty_market(adapter: BitgetAdapter) -> None:
    respx.post(ADS_URL).respond(json={"code": "00000", "data": {"dataList": None}})

    assert await adapter.fetch_offers("BGB", "UAH", "BUY") == []


@respx.mock
async def test_error_code(adapter: BitgetAdapter) -> None:
    respx.post(ADS_URL).respond(json={"code": "40001", "msg": "bad", "data": None})

    with pytest.raises(ExchangeError, match="40001"):
        await adapter.fetch_offers("USDT", "UAH", "BUY")


@respx.mock
async def test_rate_limit_is_a_block(adapter: BitgetAdapter) -> None:
    respx.post(ADS_URL).respond(status_code=429)

    with pytest.raises(ExchangeBlocked):
        await adapter.fetch_offers("USDT", "UAH", "BUY")


@respx.mock
async def test_list_markets_combines_coins_and_fiats(adapter: BitgetAdapter) -> None:
    respx.post(BASE_URL + CONFIG_PATH).respond(json=fixture("bitget_config.json"))

    markets = await adapter.list_markets()

    assert {(m.asset, m.fiat) for m in markets} == {
        (coin, fiat) for coin in ("USDT", "USDC", "BTC", "ETH", "BGB") for fiat in ("UAH", "EUR")
    }
    uah = next(m for m in markets if m.fiat == "UAH")
    assert {"monobank", "privatbank", "pumb", "abank", "raiffeisen"} <= set(uah.payment_methods)
