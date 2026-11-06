from decimal import Decimal

from pydantic import BaseModel


class Deal(BaseModel):
    """Розрахунок угоди на суму інвестиції (FR-08, FR-18)."""

    investment: Decimal
    buy_price: Decimal
    sell_price: Decimal
    quantity: Decimal  # (1.4) куплено активу
    sell_quantity: Decimal  # лишилось для продажу після комісії виводу
    buy_amount: Decimal
    sell_amount: Decimal
    fees: Decimal  # усі комісії у фіаті
    net_profit: Decimal  # (1.2)
    spread_pct: Decimal  # (1.1)
    net_spread_pct: Decimal  # (1.3)
    roi_pct: Decimal  # (1.5)
