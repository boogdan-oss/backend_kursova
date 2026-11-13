from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field

OpportunityKind = Literal["spread", "cross_exchange", "chain"]
RiskLevel = Literal["low", "medium", "high"]


class Step(BaseModel):
    """Один крок угоди; для ланцюжків — усі кроки по черзі.

    amount_in / amount_out — скільки віддаєте й отримуєте на цьому кроці для суми інвестиції:
    BUY — фіат → asset, SELL — asset → фіат, TRANSFER — asset → asset без комісії виводу.
    """

    exchange: str
    action: Literal["BUY", "SELL", "TRANSFER"]
    asset: str
    fiat: str | None = Field(default=None, description="Валюта купівлі чи продажу; None — переказ")
    price: Decimal | None = None
    payment_method: str | None = None
    amount_in: Decimal | None = None
    amount_out: Decimal | None = None


class Opportunity(BaseModel):
    kind: OpportunityKind
    asset: str
    fiat: str
    buy_exchange: str
    sell_exchange: str
    buy_price: Decimal
    sell_price: Decimal
    steps: list[Step]
    spread_pct: Decimal
    net_spread_pct: Decimal
    estimated_profit: Decimal
    max_volume: Decimal
    rr: Decimal | None = None
    risk_score: float | None = Field(default=None, ge=0, le=1)
    risk_level: RiskLevel | None = None
    ai_score: int | None = Field(default=None, ge=0, le=100)
    investment: Decimal | None = None
    detected_at: AwareDatetime
