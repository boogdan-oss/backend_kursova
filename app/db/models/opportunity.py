from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Identity, Index, SmallInteger
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Percent, hypertable, str_enum
from app.db.models.enums import OpportunityKind, RiskLevel


class Opportunity(Base):
    """Знайдена арбітражна можливість (FR-12, FR-19, FR-20)."""

    __tablename__ = "opportunities"
    __table_args__ = (
        CheckConstraint("ai_score BETWEEN 0 AND 100", name="ai_score_range"),
        CheckConstraint("risk_score BETWEEN 0 AND 1", name="risk_score_range"),
        hypertable(),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    time: Mapped[datetime] = mapped_column(primary_key=True)  # created_at / detected_at
    kind: Mapped[OpportunityKind] = mapped_column(str_enum(OpportunityKind))
    pair_id: Mapped[int] = mapped_column(ForeignKey("currency_pairs.id"))
    buy_exchange_id: Mapped[int] = mapped_column(ForeignKey("exchanges.id"))
    sell_exchange_id: Mapped[int] = mapped_column(ForeignKey("exchanges.id"))
    buy_price: Mapped[Decimal]
    sell_price: Mapped[Decimal]
    # Кроки угоди: [{exchange, action, asset, price, payment_method}, ...]
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    spread: Mapped[Decimal] = mapped_column(Percent)  # (1.1)
    net_spread: Mapped[Decimal] = mapped_column(Percent)  # (1.3)
    estimated_profit: Mapped[Decimal]  # у фіаті, для суми інвестиції
    max_volume: Mapped[Decimal]
    rr: Mapped[Decimal | None] = mapped_column(Percent)  # (1.6)
    ai_score: Mapped[int | None] = mapped_column(SmallInteger)  # (1.9), 0–100
    risk_score: Mapped[float | None]  # 0–1
    risk_level: Mapped[RiskLevel | None] = mapped_column(str_enum(RiskLevel))
    duration_sec: Mapped[int | None]  # скільки Spread тримався вище порогу (FR-20)


Index("ix_opportunities_pair_time", Opportunity.pair_id, Opportunity.time.desc())
