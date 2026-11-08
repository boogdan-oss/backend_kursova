from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Merchant(Base):
    """Продавець / покупець P2P на конкретній біржі."""

    __tablename__ = "merchants"
    __table_args__ = (
        UniqueConstraint("exchange_id", "external_id"),
        CheckConstraint("completion_rate BETWEEN 0 AND 1", name="completion_rate_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange_id: Mapped[int] = mapped_column(ForeignKey("exchanges.id", ondelete="CASCADE"))
    external_id: Mapped[str] = mapped_column(String(128))
    nickname: Mapped[str] = mapped_column(String(128))
    orders_30d: Mapped[int] = mapped_column(default=0)
    completion_rate: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
