from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field

from app.domain.merchant import Merchant

Side = Literal["BUY", "SELL"]


class Offer(BaseModel):
    """Нормалізоване P2P-оголошення — вихід будь-якого `ExchangeAdapter`."""

    exchange: str  # "binance"
    side: Side  # з погляду користувача
    asset: str  # "USDT", "BTC", ...
    fiat: str  # "UAH", "EUR", ...
    price: Decimal = Field(gt=0)
    min_amount: Decimal = Field(ge=0)  # ліміти у фіаті
    max_amount: Decimal = Field(ge=0)
    available: Decimal = Field(ge=0)  # доступно в asset
    payment_methods: list[str]  # нормалізовані ключі: "monobank", "revolut", "sepa"
    merchant: Merchant
    fetched_at: AwareDatetime
    external_id: str | None = None  # номер оголошення на біржі
