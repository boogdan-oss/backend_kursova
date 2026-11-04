"""Smart Alerts (FR-16), історія сповіщень і Watchlist (FR-17)."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Percent, TimestampMixin, str_enum
from app.db.models.enums import NotificationChannel


class AlertRule(TimestampMixin, Base):
    """Правило сповіщення. Пороги NULL — не перевіряються."""

    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    pair_id: Mapped[int] = mapped_column(ForeignKey("currency_pairs.id", ondelete="CASCADE"))
    exchange_id: Mapped[int | None] = mapped_column(ForeignKey("exchanges.id", ondelete="CASCADE"))
    payment_method_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="CASCADE")
    )
    min_spread: Mapped[Decimal | None] = mapped_column(Percent)
    min_net_spread: Mapped[Decimal | None] = mapped_column(Percent)
    min_ai_score: Mapped[int | None] = mapped_column(SmallInteger)
    min_rr: Mapped[Decimal | None] = mapped_column(Percent)
    min_net_profit: Mapped[Decimal | None]  # у фіаті пари
    channels: Mapped[list[str]] = mapped_column(
        ARRAY(String(16)), default=lambda: [NotificationChannel.IN_APP.value]
    )
    enabled: Mapped[bool] = mapped_column(default=True)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    alert_rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("alert_rules.id", ondelete="SET NULL")
    )
    # Без FK: opportunities — hypertable зі складеним PK (id, time)
    opportunity_id: Mapped[int | None] = mapped_column(BigInteger)
    channel: Mapped[NotificationChannel] = mapped_column(str_enum(NotificationChannel))
    sent_at: Mapped[datetime] = mapped_column(server_default=func.now())
    read: Mapped[bool] = mapped_column(default=False)


class WatchlistItem(TimestampMixin, Base):
    """Обрана пара — моніторинг з вищим пріоритетом. exchange_id NULL — усі біржі."""

    __tablename__ = "watchlist"
    __table_args__ = (
        UniqueConstraint("user_id", "pair_id", "exchange_id", postgresql_nulls_not_distinct=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    pair_id: Mapped[int] = mapped_column(ForeignKey("currency_pairs.id", ondelete="CASCADE"))
    exchange_id: Mapped[int | None] = mapped_column(ForeignKey("exchanges.id", ondelete="CASCADE"))
