from decimal import Decimal

from pydantic import BaseModel, Field


class Merchant(BaseModel):
    id: str
    nickname: str
    orders_30d: int = Field(ge=0)
    completion_rate: Decimal = Field(ge=0, le=1)
