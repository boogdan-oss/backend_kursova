from decimal import Decimal

import pytest

from app.engine.spreads import net_spread_pct, price_difference, roi_pct, spread_pct


@pytest.mark.parametrize(
    ("buy", "sell", "expected"),
    [
        ("62400", "62800", "0.64"),
        ("41.20", "41.85", "1.58"),
        ("41.10", "42.00", "2.19"),
    ],
)
def test_control_examples(buy: str, sell: str, expected: str) -> None:
    assert round(spread_pct(Decimal(buy), Decimal(sell)), 2) == Decimal(expected)


def test_price_difference_control_example() -> None:
    assert price_difference(Decimal(62400), Decimal(62800)) == 400


def test_spread_is_negative_when_selling_is_cheaper() -> None:
    assert spread_pct(Decimal("41.85"), Decimal("41.20")) < 0


def test_spread_is_zero_for_equal_prices() -> None:
    assert spread_pct(Decimal("41.20"), Decimal("41.20")) == 0


def test_net_spread_control_example() -> None:
    """(1.3) сценарій SRS: 810 UAH на 50 000 UAH → Net Spread 1.62 %."""
    assert net_spread_pct(Decimal(810), Decimal(50000)) == Decimal("1.62")


def test_roi_control_example() -> None:
    """(1.5) Net Profit 300 на 20 000 UAH → ROI 1.5 %."""
    assert roi_pct(Decimal(300), Decimal(20000)) == Decimal("1.5")
