import json
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.collectors.base import ExchangeBlocked, ExchangeError, ExchangeUnavailable
from app.collectors.bybit import BASE_URL, CONFIG_PATH, ITEMS_PATH, PAYMENTS_PATH, BybitAdapter
from app.collectors.ratelimit import AdaptiveRateLimiter

FIXTURES = Path(__file__).parent.parent / "fixtures"
ITEMS_URL = BASE_URL + ITEMS_PATH
PAYMENTS_URL = BASE_URL + PAYMENTS_PATH


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
async def adapter() -> AsyncIterator[BybitAdapter]:
    limiter = AdaptiveRateLimiter(initial_rps=1000, max_rps=1000, cooldown_sec=60)
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        yield BybitAdapter(limiter, client, retry_base_sec=0)


def mock_payments() -> respx.Route:
    return respx.post(PAYMENTS_URL).respond(json=fixture("bybit_payments.json"))


@respx.mock
async def test_fetch_offers_normalizes_buy_response(adapter: BybitAdapter) -> None:
    mock_payments()
    route = respx.post(ITEMS_URL).respond(json=fixture("bybit_buy.json"))

    offers = await adapter.fetch_offers("USDT", "UAH", "BUY")

    body = json.loads(route.calls.last.request.content)
    assert (body["tokenId"], body["currencyId"], body["side"]) == ("USDT", "UAH", "1")
    assert len(offers) == 20
    first = offers[0]
    assert first.exchange == "bybit"
    assert first.side == "BUY"
    assert (first.asset, first.fiat) == ("USDT", "UAH")
    assert first.price == Decimal("46.28")
    assert first.min_amount == Decimal(8830)
    assert first.max_amount == Decimal(8830)
    assert first.available == Decimal(230)
    assert first.external_id == "2118276185005187072"
    assert first.merchant.nickname == "ZalchikNalchikT"
    assert first.merchant.id == "sde4cf379adad4563a15d6ba5f6a72ea5"
    assert first.merchant.orders_30d == 96
    assert first.merchant.completion_rate == 1
    assert first.payment_methods == ["abank"]
    assert first.fetched_at.tzinfo is not None
    assert [offer.price for offer in offers] == sorted(offer.price for offer in offers)


@respx.mock
async def test_fetch_offers_normalizes_sell_response(adapter: BybitAdapter) -> None:
    mock_payments()
    route = respx.post(ITEMS_URL).respond(json=fixture("bybit_sell.json"))

    offers = await adapter.fetch_offers("USDT", "UAH", "SELL")

    assert json.loads(route.calls.last.request.content)["side"] == "0"
    first = offers[0]
    assert first.side == "SELL"
    assert first.price == Decimal("47.80")
    assert first.available == Decimal(50)
    assert first.merchant.completion_rate == Decimal("0.86")
    assert first.payment_methods == ["oschadbank"]
    assert [offer.price for offer in offers] == sorted(
        (offer.price for offer in offers), reverse=True
    )


@respx.mock
async def test_max_amount_is_limited_by_what_is_left(adapter: BybitAdapter) -> None:
    mock_payments()
    body = fixture("bybit_sell.json")
    item = body["result"]["items"][0]
    item.update(price="40.00", lastQuantity="10", minAmount="100", maxAmount="1000")
    respx.post(ITEMS_URL).respond(json=body)

    offers = await adapter.fetch_offers("USDT", "UAH", "SELL")

    assert offers[0].max_amount == 400


@respx.mock
async def test_payment_names_are_shared_with_binance_keys(adapter: BybitAdapter) -> None:
    mock_payments()
    body = fixture("bybit_buy.json")
    body["result"]["items"][0]["payments"] = ["43", "659", "60", "660", "61", "63"]
    respx.post(ITEMS_URL).respond(json=body)

    offers = await adapter.fetch_offers("USDT", "UAH", "BUY")

    assert offers[0].payment_methods == [
        "monobank",
        "monobank_iban",
        "privatbank",
        "pumb",
        "raiffeisen",
    ]


@respx.mock
async def test_unknown_payment_id_is_kept(adapter: BybitAdapter) -> None:
    mock_payments()
    body = fixture("bybit_buy.json")
    body["result"]["items"][0]["payments"] = ["999999"]
    respx.post(ITEMS_URL).respond(json=body)

    offers = await adapter.fetch_offers("USDT", "UAH", "BUY")

    assert offers[0].payment_methods == ["bybit999999"]


@respx.mock
async def test_payment_directory_is_loaded_once(adapter: BybitAdapter) -> None:
    payments = mock_payments()
    respx.post(ITEMS_URL).respond(json=fixture("bybit_buy.json"))

    await adapter.fetch_offers("USDT", "UAH", "BUY")
    await adapter.fetch_offers("USDT", "UAH", "BUY")

    assert payments.call_count == 1


@respx.mock
async def test_fetch_offers_filters_by_payment(adapter: BybitAdapter) -> None:
    mock_payments()
    respx.post(ITEMS_URL).respond(json=fixture("bybit_buy.json"))

    offers = await adapter.fetch_offers("USDT", "UAH", "BUY", payment="Mono")

    assert offers
    assert all("monobank" in offer.payment_methods for offer in offers)


@respx.mock
async def test_fetch_offers_passes_page(adapter: BybitAdapter) -> None:
    mock_payments()
    route = respx.post(ITEMS_URL).respond(json=fixture("bybit_buy.json"))

    await adapter.fetch_offers("USDT", "UAH", "BUY", page=3)

    assert json.loads(route.calls.last.request.content)["page"] == "3"


@respx.mock
async def test_empty_market(adapter: BybitAdapter) -> None:
    mock_payments()
    empty = {"ret_code": 0, "ret_msg": "SUCCESS", "result": {"count": 0, "items": []}}
    respx.post(ITEMS_URL).respond(json=empty)

    assert await adapter.fetch_offers("USDT", "XXX", "BUY") == []


@respx.mock
async def test_exchange_error_code(adapter: BybitAdapter) -> None:
    mock_payments()
    respx.post(ITEMS_URL).respond(json={"ret_code": 912100027, "ret_msg": "bad", "result": None})

    with pytest.raises(ExchangeError, match="912100027"):
        await adapter.fetch_offers("USDT", "UAH", "BUY")


@respx.mock
async def test_changed_format(adapter: BybitAdapter) -> None:
    mock_payments()
    respx.post(ITEMS_URL).respond(json={"ret_code": 0, "result": {"items": [{"price": "1"}]}})

    with pytest.raises(ExchangeError, match="формат"):
        await adapter.fetch_offers("USDT", "UAH", "BUY")


@respx.mock
async def test_non_object_body(adapter: BybitAdapter) -> None:
    mock_payments()
    respx.post(ITEMS_URL).respond(json=["unexpected"])

    with pytest.raises(ExchangeError, match="некоректна"):
        await adapter.fetch_offers("USDT", "UAH", "BUY")


@pytest.mark.parametrize("code", [10006, 10018])
@respx.mock
async def test_rate_limit_code_in_body_counts_as_block(adapter: BybitAdapter, code: int) -> None:
    mock_payments()
    route = respx.post(ITEMS_URL).respond(json={"ret_code": code, "ret_msg": "Too many visits"})

    with pytest.raises(ExchangeBlocked):
        await adapter.fetch_offers("USDT", "UAH", "BUY")

    assert route.call_count == 1
    assert adapter.limiter.state == "paused"
    assert adapter.limiter.rps == 500


@respx.mock
async def test_http_429_counts_as_block(adapter: BybitAdapter) -> None:
    mock_payments()
    respx.post(ITEMS_URL).respond(status_code=429, headers={"Retry-After": "90"})

    with pytest.raises(ExchangeBlocked):
        await adapter.fetch_offers("USDT", "UAH", "BUY")

    assert 80 < adapter.limiter.paused_for_sec <= 90


@respx.mock
async def test_html_page_counts_as_block(adapter: BybitAdapter) -> None:
    mock_payments()
    respx.post(ITEMS_URL).mock(return_value=httpx.Response(200, html="<html>captcha</html>"))

    with pytest.raises(ExchangeBlocked):
        await adapter.fetch_offers("USDT", "UAH", "BUY")


@respx.mock
async def test_server_error_is_retried(adapter: BybitAdapter) -> None:
    mock_payments()
    route = respx.post(ITEMS_URL).respond(status_code=503)

    with pytest.raises(ExchangeUnavailable):
        await adapter.fetch_offers("USDT", "UAH", "BUY")

    assert route.call_count == 3


@respx.mock
async def test_list_markets_uses_active_symbols_and_fiat_payments(adapter: BybitAdapter) -> None:
    respx.get(BASE_URL + CONFIG_PATH).respond(json=fixture("bybit_config.json"))
    mock_payments()

    markets = await adapter.list_markets()

    pairs = {(market.asset, market.fiat) for market in markets}
    assert ("USDT", "UAH") in pairs
    assert ("BTC", "UAH") in pairs
    assert ("USDT", "EUR") in pairs
    assert ("ETH", "UAH") not in pairs
    usdt = next(market for market in markets if (market.asset, market.fiat) == ("USDT", "UAH"))
    assert usdt.exchange == "bybit"
    assert {"monobank", "privatbank", "pumb", "abank"} <= set(usdt.payment_methods)
    assert len(usdt.payment_methods) == len(set(usdt.payment_methods))


def test_fees_know_withdrawal_of_common_assets() -> None:
    fees = BybitAdapter.fees()

    assert fees.exchange == "bybit"
    assert fees.p2p_fee_pct == 0
    assert set(fees.withdrawal_fees) == {"USDT", "USDC", "BTC", "ETH"}
