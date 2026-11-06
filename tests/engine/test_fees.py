from decimal import Decimal

import pytest

from app.domain import FeeSchedule
from app.engine.fees import (
    calculate_deal,
    cross_exchange_profit_pct,
    net_profit,
    trade_fee_rate,
)

NO_FEES = FeeSchedule(exchange="binance")


def test_trade_fee_rate_sums_percent_fees() -> None:
    fees = FeeSchedule(
        exchange="x",
        trade_fee_pct=Decimal("0.1"),
        p2p_fee_pct=Decimal("0.2"),
        extra_fee_pct=Decimal("0.05"),
    )

    assert trade_fee_rate(fees) == Decimal("0.0035")


def test_net_profit_control_example() -> None:
    """(1.2) 20 390 − 20 000 − 90 = 300."""
    assert net_profit(Decimal(20390), Decimal(20000), Decimal(90)) == 300


def test_deal_control_example() -> None:
    """Investment 20 000 UAH, 41.00 / 41.80, fees 90 → Net Profit 300, ROI 1.5 %."""
    deal = calculate_deal(
        Decimal(20000), Decimal("41.00"), Decimal("41.80"), NO_FEES, NO_FEES, extra_fees=Decimal(90)
    )

    assert round(deal.quantity, 2) == Decimal("487.80")
    assert round(deal.sell_amount, -1) == Decimal(20390)
    assert deal.fees == 90
    assert round(deal.net_profit) == 300
    assert round(deal.roi_pct, 1) == Decimal("1.5")


def test_srs_scenario() -> None:
    """50 000 UAH, 41.10 / 42.00: Spread ≈ 2.19 %, після комісій ≈ 810 UAH при Net Spread 1.62 %."""
    fees = Decimal(50000) * 42 / Decimal("41.10") - 50000 - 810

    deal = calculate_deal(
        Decimal(50000), Decimal("41.10"), Decimal("42.00"), NO_FEES, NO_FEES, extra_fees=fees
    )

    assert round(deal.spread_pct, 2) == Decimal("2.19")
    assert round(deal.net_spread_pct, 2) == Decimal("1.62")
    assert round(deal.net_profit) == 810


def test_percent_fees_reduce_profit_on_both_sides() -> None:
    buy_fees = FeeSchedule(exchange="a", p2p_fee_pct=Decimal("0.1"))
    sell_fees = FeeSchedule(exchange="b", p2p_fee_pct=Decimal("0.2"))

    deal = calculate_deal(Decimal(10000), Decimal(40), Decimal(41), buy_fees, sell_fees)

    # Купівля: 250 USDT, комісія 10 UAH. Продаж: 10 250 UAH, комісія 20.50 UAH
    assert deal.fees == Decimal("30.50")
    assert deal.net_profit == Decimal("219.50")


def test_withdrawal_applies_only_to_transfer_between_exchanges() -> None:
    fees = FeeSchedule(exchange="a", withdrawal_fees={"USDT": Decimal(1)})

    same_exchange = calculate_deal(Decimal(4000), Decimal(40), Decimal(41), fees, NO_FEES)
    transfer = calculate_deal(
        Decimal(4000),
        Decimal(40),
        Decimal(41),
        fees,
        NO_FEES,
        transfer_fee=fees.withdrawal_fee("USDT"),
    )

    assert same_exchange.sell_quantity == 100
    assert transfer.sell_quantity == 99
    assert transfer.fees == 41
    assert transfer.net_profit == same_exchange.net_profit - 41


def test_fee_can_eat_the_whole_profit() -> None:
    fees = FeeSchedule(exchange="a", trade_fee_pct=Decimal(1))

    deal = calculate_deal(Decimal(10000), Decimal("41.00"), Decimal("41.20"), fees, fees)

    assert deal.spread_pct > 0
    assert deal.net_profit < 0
    assert deal.net_spread_pct < 0


def test_transfer_fee_larger_than_quantity_leaves_nothing_to_sell() -> None:
    deal = calculate_deal(
        Decimal(41), Decimal(41), Decimal(42), NO_FEES, NO_FEES, transfer_fee=Decimal(5)
    )

    assert deal.sell_quantity == 0
    assert deal.net_profit == -41


@pytest.mark.parametrize(
    ("f_buy", "f_sell", "transfer"),
    [("0", "0", "0"), ("0.001", "0.002", "1"), ("0.0035", "0", "0.5"), ("0", "0.01", "0")],
)
def test_deal_matches_cross_exchange_formula(f_buy: str, f_sell: str, transfer: str) -> None:
    buy_fees = FeeSchedule(exchange="a", p2p_fee_pct=Decimal(f_buy) * 100)
    sell_fees = FeeSchedule(exchange="b", p2p_fee_pct=Decimal(f_sell) * 100)

    deal = calculate_deal(
        Decimal(20000),
        Decimal("41.00"),
        Decimal("41.80"),
        buy_fees,
        sell_fees,
        transfer_fee=Decimal(transfer),
    )

    expected = cross_exchange_profit_pct(
        Decimal("41.00"),
        Decimal("41.80"),
        deal.quantity,
        Decimal(f_buy),
        Decimal(f_sell),
        Decimal(transfer),
    )
    assert deal.net_spread_pct == pytest.approx(expected, abs=Decimal("0.0001"))


def test_cross_exchange_formula_by_hand() -> None:
    # 42 · 0.999 · 99 / (41 · 1.001 · 100) − 1 = 1.21 %
    profit = cross_exchange_profit_pct(
        Decimal(41), Decimal(42), Decimal(100), Decimal("0.001"), Decimal("0.001"), Decimal(1)
    )

    assert round(profit, 2) == Decimal("1.21")
