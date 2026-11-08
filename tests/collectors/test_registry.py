import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import respx

from app.collectors.binance import BASE_URL, SEARCH_PATH, BinanceAdapter
from app.collectors.payment_map import normalize_payment
from app.collectors.registry import (
    aclose_adapters,
    available_exchanges,
    exchange_fees,
    get_adapter,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "binance_sell.json"


@pytest.fixture(autouse=True)
async def clean_registry() -> AsyncIterator[None]:
    yield
    await aclose_adapters()


def test_exchanges_are_registered() -> None:
    assert set(available_exchanges()) == {"binance", "bybit", "okx", "bitget", "mexc", "htx"}


def test_fees_do_not_need_an_adapter_instance() -> None:
    fees = exchange_fees()

    assert set(fees) == set(available_exchanges())
    assert fees["bybit"].withdrawal_fee("USDT") > 0
    assert fees["binance"].withdrawal_fee("USDT") > 0


async def test_get_adapter_returns_one_instance_per_exchange() -> None:
    adapter = get_adapter("binance")

    assert isinstance(adapter, BinanceAdapter)
    assert get_adapter("binance") is adapter


async def test_aclose_adapters_forgets_instances() -> None:
    adapter = get_adapter("binance")

    await aclose_adapters()

    assert get_adapter("binance") is not adapter


@respx.mock
async def test_registered_adapter_fetches_through_its_own_client() -> None:
    body = json.loads(FIXTURE.read_text(encoding="utf-8"))
    respx.post(BASE_URL + SEARCH_PATH).respond(json=body)

    offers = await get_adapter("binance").fetch_offers("USDT", "UAH", "SELL")

    assert len(offers) == 20
    assert {offer.exchange for offer in offers} == {"binance"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Monobank", "monobank"),
        ("Mono", "monobank"),
        ("monobank", "monobank"),
        ("PrivatBank", "privatbank"),
        ("PUMBBank", "pumb"),
        ("RaiffeisenBankAval", "raiffeisen"),
        ("OTPBankNew", "otpbank"),
        ("Monobank (card)", "monobank"),
        ("PUMB (CARD)", "pumb"),
        ("A-Bank (CARD)", "abank"),
        ("OSCHAD BANK (CARD)", "oschadbank"),
        ("Raiffeisen Bank Aval (Card)", "raiffeisen"),
        ("MONOBANK (IBAN)", "monobank_iban"),
        ("Privatbank (card)", "privatbank"),
        ("Privat Bank", "privatbank"),
        ("Monobank (IBAN)", "monobank_iban"),
        ("Raiffeisenbank", "raiffeisen"),
        ("A-Bank", "abank"),
        ("SenseSuperApp", "sense"),
        ("Monobankiban", "monobank_iban"),
        ("PrivatBankIBAN", "privatbank_iban"),
        ("SomeNewBank", "somenewbank"),
    ],
)
def test_normalize_payment(raw: str, expected: str) -> None:
    assert normalize_payment(raw) == expected
