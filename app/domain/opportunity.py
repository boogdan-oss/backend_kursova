from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field

OpportunityKind = Literal["spread", "cross_exchange", "chain"]
RiskLevel = Literal["low", "medium", "high"]


class Step(BaseModel):
    """Один крок угоди; для ланцюжків — усі кроки по черзі."""

    exchange: str
    action: Literal["BUY", "SELL", "TRANSFER"]
    asset: str
    price: Decimal | None = None  # None для TRANSFER
    payment_method: str | None = None  # банк / спосіб оплати


class Opportunity(BaseModel):
    kind: OpportunityKind
    asset: str
    fiat: str
    buy_exchange: str
    sell_exchange: str
    buy_price: Decimal
    sell_price: Decimal
    steps: list[Step]
    spread_pct: Decimal  # (1.1), до комісій
    net_spread_pct: Decimal  # (1.3), після комісій
    estimated_profit: Decimal  # у фіаті, для суми інвестиції
    max_volume: Decimal
    rr: Decimal | None = None  # (1.6)
    risk_score: float | None = Field(default=None, ge=0, le=1)  # від AI
    risk_level: RiskLevel | None = None
    ai_score: int | None = Field(default=None, ge=0, le=100)  # (1.9)
    detected_at: AwareDatetime
