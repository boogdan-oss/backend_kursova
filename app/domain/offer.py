from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field

from app.domain.merchant import Merchant

Side = Literal["BUY", "SELL"]


class Offer(BaseModel):
    """Нормалізоване P2P-оголошення — вихід будь-якого `ExchangeAdapter`."""

    exchange: str
    side: Side
    asset: str
    fiat: str
    price: Decimal = Field(gt=0)
    min_amount: Decimal = Field(ge=0)
    max_amount: Decimal = Field(ge=0)
    available: Decimal = Field(ge=0)
    payment_methods: list[str]
    merchant: Merchant
    fetched_at: AwareDatetime
    external_id: str | None = None
