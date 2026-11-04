from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, Identity, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Percent, hypertable


class MarketStats(Base):
    """Агрегована ціна пари на біржі (≈ MarketPrice у SRS).

    P2P-відповідність SRS: ask_price → buy_price (найкраща ціна купівлі),
    bid_price → sell_price (найкраща ціна, за якою можна продати).
    """

    __tablename__ = "market_stats"
    __table_args__ = hypertable()

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    time: Mapped[datetime] = mapped_column(primary_key=True)
    exchange_id: Mapped[int] = mapped_column(ForeignKey("exchanges.id"))
    pair_id: Mapped[int] = mapped_column(ForeignKey("currency_pairs.id"))
    # NULL — по всіх способах оплати
    payment_method_id: Mapped[int | None] = mapped_column(ForeignKey("payment_methods.id"))
    buy_price: Mapped[Decimal | None]  # ask; NULL — немає оголошень на стороні
    sell_price: Mapped[Decimal | None]  # bid
    spread: Mapped[Decimal | None] = mapped_column(Percent)  # (1.1), %
    volume: Mapped[Decimal]  # сумарно доступно в активі
    offers_count_buy: Mapped[int] = mapped_column(default=0)
    offers_count_sell: Mapped[int] = mapped_column(default=0)


Index(
    "ix_market_stats_pair_exchange_time",
    MarketStats.pair_id,
    MarketStats.exchange_id,
    MarketStats.time.desc(),
)


class SpreadHistory(Base):
    """Історія міжбіржового спреду для пари (FR-06)."""

    __tablename__ = "spread_history"
    __table_args__ = hypertable()

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    time: Mapped[datetime] = mapped_column(primary_key=True)
    pair_id: Mapped[int] = mapped_column(ForeignKey("currency_pairs.id"))
    buy_exchange_id: Mapped[int] = mapped_column(ForeignKey("exchanges.id"))
    sell_exchange_id: Mapped[int] = mapped_column(ForeignKey("exchanges.id"))
    buy_price: Mapped[Decimal]
    sell_price: Mapped[Decimal]
    spread: Mapped[Decimal] = mapped_column(Percent)  # (1.1), до комісій
    net_spread: Mapped[Decimal] = mapped_column(Percent)  # (1.3), після комісій


Index("ix_spread_history_pair_time", SpreadHistory.pair_id, SpreadHistory.time.desc())
