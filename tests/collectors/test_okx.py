from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
import respx

from app.collectors.base import ExchangeBlocked, ExchangeError
from app.collectors.fiats import ISO_4217
from app.collectors.okx import (
    ASSETS,
    BASE_URL,
    BOOKS_PATH,
    PAYMENTS_PATH,
    PROBE_ATTEMPTS,
    OkxAdapter,
)
from app.collectors.ratelimit import AdaptiveRateLimiter
from tests.collectors.exchange_cases import fixture, make_adapter

BOOKS_URL = BASE_URL + BOOKS_PATH


@pytest.fixture
async def adapter() -> AsyncIterator[OkxAdapter]:
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        yield make_adapter(OkxAdapter, client)


def by_side(request: httpx.Request) -> httpx.Response:
    side = request.url.params["side"]
    return httpx.Response(200, json=fixture("okx_buy.json" if side == "sell" else "okx_sell.json"))


@respx.mock
async def test_buy_side_asks_for_sellers_and_normalizes(adapter: OkxAdapter) -> None:
    route = respx.get(BOOKS_URL).mock(side_effect=by_side)

    offers = await adapter.fetch_offers("USDT", "UAH", "BUY")

    params = route.calls.last.request.url.params
    assert (params["side"], params["baseCurrency"], params["quoteCurrency"]) == (
        "sell",
        "usdt",
        "uah",
    )
    assert params["limit"] == "20"
    assert len(offers) == 20
    first = offers[0]
    assert (first.exchange, first.side, first.asset, first.fiat) == ("okx", "BUY", "USDT", "UAH")
    assert first.price == Decimal("46.70")
    assert first.available == 100
    assert (first.min_amount, first.max_amount) == (2181, 2181)
    assert first.merchant.id == "67e5f74b60"
    assert first.merchant.nickname == "P2P_SantexniK"
    assert first.merchant.completion_rate == Decimal("0.9893")
    assert first.external_id == "261108195229871"
    assert first.payment_methods == [
        "pumb",
        "monobank",
        "abank",
        "raiffeisen",
        "bankvlasnyirakhunok",
    ]
    assert [offer.price for offer in offers] == sorted(offer.price for offer in offers)


@respx.mock
async def test_sell_side_asks_for_buyers(adapter: OkxAdapter) -> None:
    respx.get(BOOKS_URL).mock(side_effect=by_side)

    offers = await adapter.fetch_offers("USDT", "UAH", "SELL")

    assert offers[0].side == "SELL"
    assert offers[0].price == Decimal("49.00")
    assert [offer.price for offer in offers] == sorted((o.price for o in offers), reverse=True)


@respx.mock
async def test_second_page_asks_for_more_and_skips_first(adapter: OkxAdapter) -> None:
    route = respx.get(BOOKS_URL).mock(side_effect=by_side)

    offers = await adapter.fetch_offers("USDT", "UAH", "BUY", page=2)

    assert route.calls.last.request.url.params["limit"] == "40"
    assert offers == []


@respx.mock
async def test_unsupported_pair_is_empty_market(adapter: OkxAdapter) -> None:
    respx.get(BOOKS_URL).respond(json={"code": 17007, "data": {}, "msg": "not supported"})

    assert await adapter.fetch_offers("USDT", "XYZ", "BUY") == []


@respx.mock
async def test_other_error_code(adapter: OkxAdapter) -> None:
    respx.get(BOOKS_URL).respond(json={"code": 50011, "data": None, "msg": "busy"})

    with pytest.raises(ExchangeError, match="50011"):
        await adapter.fetch_offers("USDT", "UAH", "BUY")


@respx.mock
async def test_rate_limit_is_a_block(adapter: OkxAdapter) -> None:
    respx.get(BOOKS_URL).respond(status_code=429)

    with pytest.raises(ExchangeBlocked):
        await adapter.fetch_offers("USDT", "UAH", "BUY")


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 1e-6)


@respx.mock
async def test_list_markets_waits_out_block_and_continues() -> None:
    clock = FakeClock()
    calls = {"count": 0}

    def books(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 3:
            return httpx.Response(429)
        if request.url.params["quoteCurrency"] == "uah":
            return httpx.Response(200, json=fixture("okx_buy.json"))
        return httpx.Response(200, json={"code": 17007, "data": {}, "msg": "not supported"})

    respx.get(BOOKS_URL).mock(side_effect=books)
    respx.get(BASE_URL + PAYMENTS_PATH).respond(json=fixture("okx_payments.json"))
    limiter = AdaptiveRateLimiter(1000, 1000, 60, clock, clock.sleep)

    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        markets = await OkxAdapter(limiter, client, retry_base_sec=0).list_markets()

    assert calls["count"] == len(ISO_4217) + 1
    assert {market.fiat for market in markets} == {"UAH"}


@respx.mock
async def test_list_markets_gives_up_after_repeated_blocks(adapter: OkxAdapter) -> None:
    clock = FakeClock()
    route = respx.get(BOOKS_URL).respond(status_code=429)
    adapter.limiter = AdaptiveRateLimiter(1000, 1000, 60, clock, clock.sleep)

    with pytest.raises(ExchangeBlocked):
        await adapter.list_markets()

    assert route.call_count == PROBE_ATTEMPTS


@respx.mock
async def test_list_markets_probes_fiats(adapter: OkxAdapter) -> None:
    def books(request: httpx.Request) -> httpx.Response:
        if request.url.params["quoteCurrency"] in ("uah", "pln"):
            return httpx.Response(200, json=fixture("okx_buy.json"))
        return httpx.Response(200, json={"code": 17007, "data": {}, "msg": "not supported"})

    probes = respx.get(BOOKS_URL).mock(side_effect=books)
    templates = respx.get(BASE_URL + PAYMENTS_PATH).respond(json=fixture("okx_payments.json"))

    markets = await adapter.list_markets()

    assert probes.call_count == len(ISO_4217)
    assert templates.call_count == 2
    assert {(market.asset, market.fiat) for market in markets} == {
        (asset, fiat) for asset in ASSETS for fiat in ("UAH", "PLN")
    }
    uah = next(market for market in markets if market.fiat == "UAH")
    assert {"monobank", "privatbank", "pumb", "monobank_iban"} <= set(uah.payment_methods)
