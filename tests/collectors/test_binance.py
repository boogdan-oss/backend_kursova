import json
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.collectors.base import ExchangeBlocked, ExchangeError, ExchangeUnavailable
from app.collectors.binance import (
    BASE_URL,
    CONFIG_PATH,
    EXTRA_FIATS,
    FIAT_LIST_PATH,
    SEARCH_PATH,
    BinanceAdapter,
)
from app.collectors.ratelimit import AdaptiveRateLimiter

FIXTURES = Path(__file__).parent.parent / "fixtures"
SEARCH_URL = BASE_URL + SEARCH_PATH


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
async def adapter() -> AsyncIterator[BinanceAdapter]:
    limiter = AdaptiveRateLimiter(initial_rps=1000, max_rps=1000, cooldown_sec=60)
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        yield BinanceAdapter(limiter, client, retry_base_sec=0)


@respx.mock
async def test_fetch_offers_normalizes_buy_response(adapter: BinanceAdapter) -> None:
    route = respx.post(SEARCH_URL).respond(json=fixture("binance_buy.json"))

    offers = await adapter.fetch_offers("USDT", "UAH", "BUY")

    body = json.loads(route.calls.last.request.content)
    assert (body["asset"], body["fiat"], body["tradeType"]) == ("USDT", "UAH", "BUY")
    assert len(offers) == 20
    first = offers[0]
    assert first.exchange == "binance"
    assert first.side == "BUY"
    assert (first.asset, first.fiat) == ("USDT", "UAH")
    assert first.price == Decimal("46.10")
    assert first.min_amount == Decimal(528)
    assert first.available == Decimal("109.94")
    assert first.external_id == "13951468000846204928"
    assert first.merchant.nickname == "ExTurk"
    assert first.merchant.id.startswith("s45303ffc8")
    assert first.merchant.orders_30d == 36849
    assert first.merchant.completion_rate == Decimal("1.0")
    assert first.payment_methods == ["abank", "monobank", "privatbank"]
    assert first.fetched_at.tzinfo is not None
    assert [offer.price for offer in offers] == sorted(offer.price for offer in offers)


@respx.mock
async def test_fetch_offers_normalizes_sell_response(adapter: BinanceAdapter) -> None:
    respx.post(SEARCH_URL).respond(json=fixture("binance_sell.json"))

    offers = await adapter.fetch_offers("USDT", "UAH", "SELL")

    first = offers[0]
    assert first.side == "SELL"
    assert first.price == Decimal("49.00")
    assert first.min_amount == Decimal(350000)
    # Заявлено 3 670 000, але залишку оголошення вистачає лише на 1 771 334
    assert first.max_amount == Decimal(1771334)
    assert [offer.price for offer in offers] == sorted(
        (offer.price for offer in offers), reverse=True
    )


@respx.mock
async def test_limits_are_in_fiat_not_in_asset(adapter: BinanceAdapter) -> None:
    respx.post(SEARCH_URL).respond(json=fixture("binance_buy.json"))

    offers = await adapter.fetch_offers("USDT", "UAH", "BUY")

    # 11.45 USDT × 46.10 = 528 UAH: у полях мають бути гривні
    assert offers[0].max_amount == Decimal(528)
    assert offers[2].min_amount == Decimal(15000)


@respx.mock
async def test_fetch_offers_passes_page(adapter: BinanceAdapter) -> None:
    route = respx.post(SEARCH_URL).respond(json=fixture("binance_buy.json"))

    await adapter.fetch_offers("USDT", "UAH", "BUY", page=3)

    assert json.loads(route.calls.last.request.content)["page"] == 3


@respx.mock
async def test_fetch_offers_filters_by_payment(adapter: BinanceAdapter) -> None:
    respx.post(SEARCH_URL).respond(json=fixture("binance_buy.json"))

    offers = await adapter.fetch_offers("USDT", "UAH", "BUY", payment="Mono")

    assert offers
    assert all("monobank" in offer.payment_methods for offer in offers)


@respx.mock
async def test_fetch_offers_empty_market(adapter: BinanceAdapter) -> None:
    empty = {"code": "000000", "data": [], "total": 0, "success": True}
    respx.post(SEARCH_URL).respond(json=empty)

    assert await adapter.fetch_offers("NOPE", "UAH", "BUY") == []


@respx.mock
async def test_fetch_offers_raises_on_exchange_error_code(adapter: BinanceAdapter) -> None:
    respx.post(SEARCH_URL).respond(json={"code": "000002", "message": "bad", "data": None})

    with pytest.raises(ExchangeError, match="000002"):
        await adapter.fetch_offers("USDT", "UAH", "BUY")


@respx.mock
async def test_fetch_offers_raises_on_changed_format(adapter: BinanceAdapter) -> None:
    respx.post(SEARCH_URL).respond(json={"code": "000000", "data": [{"adv": {}}]})

    with pytest.raises(ExchangeError, match="формат"):
        await adapter.fetch_offers("USDT", "UAH", "BUY")


@respx.mock
async def test_fetch_offers_raises_on_non_object_body(adapter: BinanceAdapter) -> None:
    respx.post(SEARCH_URL).respond(json=["unexpected"])

    with pytest.raises(ExchangeError, match="некоректна"):
        await adapter.fetch_offers("USDT", "UAH", "BUY")


@pytest.mark.parametrize("status_code", [403, 418, 429])
@respx.mock
async def test_block_pauses_limiter_and_is_not_retried(
    adapter: BinanceAdapter, status_code: int
) -> None:
    route = respx.post(SEARCH_URL).respond(status_code=status_code, headers={"Retry-After": "90"})

    with pytest.raises(ExchangeBlocked):
        await adapter.fetch_offers("USDT", "UAH", "BUY")

    assert route.call_count == 1
    assert adapter.limiter.state == "paused"
    assert 80 < adapter.limiter.paused_for_sec <= 90
    assert adapter.limiter.rps == 500


@respx.mock
async def test_captcha_page_counts_as_block(adapter: BinanceAdapter) -> None:
    captcha = httpx.Response(202, html="<html>captcha</html>")
    respx.post(SEARCH_URL).mock(return_value=captcha)

    with pytest.raises(ExchangeBlocked):
        await adapter.fetch_offers("USDT", "UAH", "BUY")


@respx.mock
async def test_server_error_is_retried_until_success(adapter: BinanceAdapter) -> None:
    route = respx.post(SEARCH_URL).mock(
        side_effect=[httpx.Response(502), httpx.Response(200, json=fixture("binance_buy.json"))]
    )

    offers = await adapter.fetch_offers("USDT", "UAH", "BUY")

    assert route.call_count == 2
    assert len(offers) == 20


@respx.mock
async def test_server_error_gives_up_after_retries(adapter: BinanceAdapter) -> None:
    route = respx.post(SEARCH_URL).respond(status_code=503)

    with pytest.raises(ExchangeUnavailable):
        await adapter.fetch_offers("USDT", "UAH", "BUY")

    assert route.call_count == 3


@respx.mock
async def test_network_error_is_retried(adapter: BinanceAdapter) -> None:
    route = respx.post(SEARCH_URL).mock(side_effect=httpx.ConnectTimeout("timeout"))

    with pytest.raises(ExchangeUnavailable):
        await adapter.fetch_offers("USDT", "UAH", "BUY")

    assert route.call_count == 3


@respx.mock
async def test_client_error_is_not_retried(adapter: BinanceAdapter) -> None:
    route = respx.post(SEARCH_URL).respond(status_code=400)

    with pytest.raises(ExchangeError):
        await adapter.fetch_offers("USDT", "UAH", "BUY")

    assert route.call_count == 1


@respx.mock
async def test_list_markets_reads_p2p_area_of_every_fiat(adapter: BinanceAdapter) -> None:
    respx.post(BASE_URL + FIAT_LIST_PATH).respond(json=fixture("binance_fiat_list.json"))
    config = fixture("binance_portal_config.json")
    empty = {"code": "000000", "data": {"fiat": None, "areas": []}, "success": True}

    def config_for(request: httpx.Request) -> httpx.Response:
        fiat = json.loads(request.content)["fiat"]
        return httpx.Response(200, json=config if fiat == "UAH" else empty)

    route = respx.post(BASE_URL + CONFIG_PATH).mock(side_effect=config_for)

    markets = await adapter.list_markets()

    requested = {json.loads(call.request.content)["fiat"] for call in route.calls}
    assert requested == {"UAH", "USD", *EXTRA_FIATS}
    assert {market.fiat for market in markets} == {"UAH"}
    assert {market.asset for market in markets} >= {"USDT", "BTC", "ETH"}
    usdt = next(market for market in markets if market.asset == "USDT")
    assert usdt.exchange == "binance"
    # Лише область P2P: Monobank IBAN є тільки в EXPRESS
    assert usdt.payment_methods == ["monobank", "privatbank", "abank", "pumb"]


@respx.mock
async def test_list_markets_skips_fiat_listed_as_asset(adapter: BinanceAdapter) -> None:
    respx.post(BASE_URL + FIAT_LIST_PATH).respond(json=fixture("binance_fiat_list.json"))
    config = fixture("binance_portal_config.json")
    for area in config["data"]["areas"]:
        for side in area["tradeSides"]:
            side["assets"].append({"asset": "USD", "description": "US Dollar"})
    respx.post(BASE_URL + CONFIG_PATH).respond(json=config)

    markets = await adapter.list_markets()

    assert "USD" not in {market.asset for market in markets}
    assert "USDT" in {market.asset for market in markets}


@respx.mock
async def test_list_markets_survives_one_broken_fiat(adapter: BinanceAdapter) -> None:
    respx.post(BASE_URL + FIAT_LIST_PATH).respond(json=fixture("binance_fiat_list.json"))
    config = fixture("binance_portal_config.json")

    def config_for(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["fiat"] == "USD":
            return httpx.Response(200, json={"code": "000002", "data": None})
        return httpx.Response(200, json=config)

    respx.post(BASE_URL + CONFIG_PATH).mock(side_effect=config_for)

    markets = await adapter.list_markets()

    assert "UAH" in {market.fiat for market in markets}
    assert "USD" not in {market.fiat for market in markets}


@respx.mock
async def test_list_markets_stops_when_blocked(adapter: BinanceAdapter) -> None:
    respx.post(BASE_URL + FIAT_LIST_PATH).respond(json=fixture("binance_fiat_list.json"))
    route = respx.post(BASE_URL + CONFIG_PATH).respond(status_code=429)

    with pytest.raises(ExchangeBlocked):
        await adapter.list_markets()

    assert route.call_count == 1
