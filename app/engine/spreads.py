from decimal import Decimal

PERCENT_PRECISION = Decimal("0.0001")
MONEY_PRECISION = Decimal("0.00000001")


def _percent(value: Decimal) -> Decimal:
    return value.quantize(PERCENT_PRECISION)


def round_money(value: Decimal) -> Decimal:
    """Прибирає шум ділення Decimal: 40.99999999999999999999999999 → 41."""
    return value.quantize(MONEY_PRECISION)


def price_difference(buy_price: Decimal, sell_price: Decimal) -> Decimal:
    """FR-03: абсолютна різниця цін."""
    return sell_price - buy_price


def spread_pct(buy_price: Decimal, sell_price: Decimal) -> Decimal:
    """(1.1) Spread = (P_sell − P_buy) / P_buy × 100 %."""
    return _percent((sell_price - buy_price) / buy_price * 100)


def net_spread_pct(net_profit: Decimal, buy_amount: Decimal) -> Decimal:
    """(1.3) Net Spread = Net Profit / Buy Amount × 100 %."""
    return _percent(net_profit / buy_amount * 100)


def roi_pct(net_profit: Decimal, investment: Decimal) -> Decimal:
    """(1.5) ROI = Net Profit / Investment × 100 %."""
    return _percent(net_profit / investment * 100)
