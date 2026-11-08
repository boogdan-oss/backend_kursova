from decimal import Decimal

from pydantic import BaseModel


class Deal(BaseModel):
    """Розрахунок угоди на суму інвестиції (FR-08, FR-18)."""

    investment: Decimal
    buy_price: Decimal
    sell_price: Decimal
    quantity: Decimal
    sell_quantity: Decimal
    buy_amount: Decimal
    sell_amount: Decimal
    fees: Decimal
    net_profit: Decimal
    spread_pct: Decimal
    net_spread_pct: Decimal
    roi_pct: Decimal
