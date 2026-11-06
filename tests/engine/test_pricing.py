from decimal import Decimal

import pytest

from app.engine.pricing import (
    average_price,
    effective_price,
    fill_order,
    market_snapshot,
    quantity_for,
    slippage_pct,
    typical_amount,
)
from tests.factories import FETCHED_AT, make_offer

WIDE_LIMITS = {"min_amount": "1", "max_amount": "10000000"}


def test_effective_price_of_empty_side_is_none() -> None:
    assert effective_price([]) is None


def test_single_offer_is_its_own_effective_price() -> None:
    assert effective_price([make_offer("41.20")]) == Decimal("41.20")


def test_buy_side_uses_median_of_five_cheapest() -> None:
    prices = ["41.50", "41.10", "41.40", "41.20", "41.30", "45.00", "46.00"]

    assert effective_price([make_offer(price) for price in prices]) == Decimal("41.30")


def test_sell_side_uses_median_of_five_most_expensive() -> None:
    prices = ["40.00", "41.50", "41.10", "41.40", "41.20", "41.30", "39.00"]

    offers = [make_offer(price, "SELL") for price in prices]

    assert effective_price(offers) == Decimal("41.30")


def test_even_number_of_offers_averages_two_middle_prices() -> None:
    offers = [make_offer(price) for price in ["41.00", "41.10", "41.20", "41.30"]]

    assert effective_price(offers) == Decimal("41.15")


def test_fake_lot_does_not_move_effective_price() -> None:
    honest = [make_offer(price) for price in ["41.20", "41.25", "41.30", "41.35", "41.40"]]
    with_fake = [make_offer("1.00"), *honest]

    assert effective_price(with_fake) == Decimal("41.25")


def test_quantity_control_example() -> None:
    """(1.4) Investment 20 000 UAH, P_buy 41.00 → Q = 487.80 USDT."""
    assert round(quantity_for(Decimal(20000), Decimal("41.00")), 2) == Decimal("487.80")


def test_average_price_control_example() -> None:
    """(1.7) 50 USDT @ 41.20 і 450 USDT @ 41.30 → P_avg ≈ 41.29."""
    levels = [(Decimal("41.20"), Decimal(50)), (Decimal("41.30"), Decimal(450))]

    assert average_price(levels) == Decimal("41.29")


def test_slippage_control_example() -> None:
    """(1.8) P_avg 41.29, P_best 41.20 → Slippage ≈ 0.22 %."""
    assert round(slippage_pct(Decimal("41.29"), Decimal("41.20")), 2) == Decimal("0.22")


def test_fill_order_control_example() -> None:
    """Рівні 50 @ 41.20 і 600 @ 41.30, купівля 500 USDT (CLAUDE.md, розділ 6)."""
    offers = [
        make_offer("41.30", available="600", **WIDE_LIMITS),
        make_offer("41.20", available="50", **WIDE_LIMITS),
    ]

    fill = fill_order(offers, Decimal(500))

    assert fill is not None
    assert fill.complete is True
    assert fill.quantity == 500
    assert fill.best_price == Decimal("41.20")
    assert fill.average_price == Decimal("41.29")
    assert round(fill.slippage_pct, 2) == Decimal("0.22")


def test_fill_order_within_first_level_has_no_slippage() -> None:
    fill = fill_order([make_offer("41.20", available="600", **WIDE_LIMITS)], Decimal(500))

    assert fill is not None
    assert fill.average_price == Decimal("41.20")
    assert fill.slippage_pct == 0


def test_fill_order_has_no_division_noise() -> None:
    quantity = quantity_for(Decimal(20000), Decimal(41))

    fill = fill_order([make_offer("41", available="5000", **WIDE_LIMITS)], quantity)

    assert fill is not None
    assert str(fill.average_price) == "41.00000000"


def test_fill_order_on_sell_side_starts_from_highest_price() -> None:
    offers = [
        make_offer("41.00", "SELL", available="600", **WIDE_LIMITS),
        make_offer("41.10", "SELL", available="100", **WIDE_LIMITS),
    ]

    fill = fill_order(offers, Decimal(200))

    assert fill is not None
    assert fill.best_price == Decimal("41.10")
    assert fill.average_price == Decimal("41.05")
    assert fill.slippage_pct > 0


def test_fill_order_reports_shortage() -> None:
    fill = fill_order([make_offer("41.20", available="50", **WIDE_LIMITS)], Decimal(500))

    assert fill is not None
    assert fill.complete is False
    assert fill.quantity == 50


def test_fill_order_respects_fiat_limit_of_offer() -> None:
    # Доступно 600 USDT, але за одну угоду оголошення приймає лише 4120 UAH = 100 USDT
    offers = [
        make_offer("41.20", available="600", min_amount="1", max_amount="4120"),
        make_offer("41.30", available="600", **WIDE_LIMITS),
    ]

    fill = fill_order(offers, Decimal(200))

    assert fill is not None
    assert fill.average_price == Decimal("41.25")


def test_fill_order_skips_offer_with_minimum_above_remaining_amount() -> None:
    big_lot = make_offer("41.10", available="1000", min_amount="350000", max_amount="900000")
    regular = make_offer("41.20", available="1000", **WIDE_LIMITS)

    fill = fill_order([big_lot, regular], Decimal(100))

    assert fill is not None
    assert fill.average_price == Decimal("41.20")


def test_fill_order_without_usable_offers() -> None:
    assert fill_order([], Decimal(100)) is None
    big_lot = make_offer("41.10", min_amount="350000", max_amount="900000")
    assert fill_order([big_lot], Decimal(1)) is None


def test_typical_amount_is_median_of_minimums() -> None:
    offers = [make_offer(min_amount=value) for value in ("500", "1000", "20000")]

    assert typical_amount(offers) == Decimal(1000)
    assert typical_amount([]) is None


def test_snapshot_uses_only_reliable_merchants_for_price() -> None:
    reliable = make_offer("41.20")
    newcomer = make_offer("30.00", orders=3)
    sell = make_offer("42.00", "SELL")

    snapshot = market_snapshot("binance", "USDT", "UAH", [reliable, newcomer], [sell], FETCHED_AT)

    assert snapshot.buy_price == Decimal("41.20")
    assert snapshot.sell_price == Decimal("42.00")
    assert snapshot.spread_pct == Decimal("1.9417")
    assert snapshot.offers_count_buy == 2
    assert snapshot.offers_count_sell == 1
    assert snapshot.volume == Decimal(300)


def test_snapshot_without_one_side_has_no_spread() -> None:
    snapshot = market_snapshot("binance", "USDT", "UAH", [make_offer()], [], FETCHED_AT)

    assert snapshot.buy_price == Decimal("41.20")
    assert snapshot.sell_price is None
    assert snapshot.spread_pct is None


def test_snapshot_of_empty_market() -> None:
    snapshot = market_snapshot("binance", "USDT", "UAH", [], [], FETCHED_AT, "monobank")

    assert snapshot.buy_price is None
    assert snapshot.volume == 0
    assert snapshot.payment_method == "monobank"


def test_average_price_rejects_empty_levels() -> None:
    with pytest.raises(ValueError, match="рівн"):
        average_price([])
