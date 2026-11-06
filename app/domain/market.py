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
    payment_method: str | None = None  # None — по всіх способах оплати
    buy_price: Decimal | None = None  # None — немає оголошень на стороні
    sell_price: Decimal | None = None
    spread_pct: Decimal | None = None  # (1.1)
    volume: Decimal = Decimal(0)  # доступно в asset
    offers_count_buy: int = Field(default=0, ge=0)
    offers_count_sell: int = Field(default=0, ge=0)
    fetched_at: AwareDatetime


class Freshness(BaseModel):
    """Наскільки актуальні дані пари на біржі."""

    exchange: str
    buy_fetched_at: AwareDatetime | None = None
    sell_fetched_at: AwareDatetime | None = None
    age_sec: float | None = None  # вік найстарішої сторони; None — ще не збиралось
    stale: bool = True


class CollectorStatus(BaseModel):
    """Стан collector по біржі, який API читає з Redis."""

    exchange: str
    rps: float  # поточна швидкість rate limiter
    rps_ceiling: float  # стеля, знайдена на блокуваннях біржі
    limiter_state: Literal["ok", "paused", "cooldown"]
    paused_for_sec: float
    tasks: dict[str, int]  # кількість задач за пріоритетами
    requests_ok: int  # лічильники від запуску collector
    requests_failed: int
    rows_written: int
    started_at: AwareDatetime
    updated_at: AwareDatetime
