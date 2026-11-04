from decimal import Decimal

from pydantic import BaseModel, Field


class FeeSchedule(BaseModel):
    """Комісії біржі для формули (1.2): Fees = біржа + P2P + вивід + мережа + додаткові."""

    exchange: str
    trade_fee_pct: Decimal = Field(default=Decimal(0), ge=0)  # комісія біржі, %
    p2p_fee_pct: Decimal = Field(default=Decimal(0), ge=0)  # P2P-комісія, %
    # Фіксована комісія виводу (разом із мережевою) в одиницях активу: {"USDT": 1}
    withdrawal_fees: dict[str, Decimal] = Field(default_factory=dict)
    extra_fee_pct: Decimal = Field(default=Decimal(0), ge=0)  # додаткові, %

    def withdrawal_fee(self, asset: str) -> Decimal:
        return self.withdrawal_fees.get(asset, Decimal(0))
