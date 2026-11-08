from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field


class P2PMarket(BaseModel):
    """Пара, яку підтримує біржа, і способи оплати для неї (`list_markets`)."""

    exchange: str
    asset: str
    fiat: str
    payment_methods: list[str]


class MarketSnapshot(BaseModel):
    """Ефективні ціни пари на біржі (≈ MarketPrice SRS).

    buy_price — ask (найкраща ціна купівлі), sell_price — bid (найкраща ціна продажу).
    """

    exchange: str
    asset: str
    fiat: str
    payment_method: str | None = None
    buy_price: Decimal | None = None
    sell_price: Decimal | None = None
    spread_pct: Decimal | None = None
    volume: Decimal = Decimal(0)
    offers_count_buy: int = Field(default=0, ge=0)
    offers_count_sell: int = Field(default=0, ge=0)
    fetched_at: AwareDatetime


class Freshness(BaseModel):
    """Наскільки актуальні дані пари на біржі."""

    exchange: str
    buy_fetched_at: AwareDatetime | None = None
    sell_fetched_at: AwareDatetime | None = None
    age_sec: float | None = None
    stale: bool = True


class CollectorStatus(BaseModel):
    """Стан collector по біржі, який API читає з Redis."""

    exchange: str
    rps: float
    rps_ceiling: float
    limiter_state: Literal["ok", "paused", "cooldown"]
    paused_for_sec: float
    tasks: dict[str, int]
    requests_ok: int
    requests_failed: int
    rows_written: int
    started_at: AwareDatetime
    updated_at: AwareDatetime
