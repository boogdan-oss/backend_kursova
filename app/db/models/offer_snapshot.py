from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, Identity, Index, String
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, hypertable, str_enum
from app.db.models.enums import Side


class OfferSnapshot(Base):
    """Знімок P2P-оголошення на момент збору (hypertable, retention 30 днів)."""

    __tablename__ = "offer_snapshots"
    __table_args__ = hypertable()

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    time: Mapped[datetime] = mapped_column(primary_key=True)
    exchange_id: Mapped[int] = mapped_column(ForeignKey("exchanges.id"))
    pair_id: Mapped[int] = mapped_column(ForeignKey("currency_pairs.id"))
    side: Mapped[Side] = mapped_column(str_enum(Side, length=4))
    price: Mapped[Decimal]
    min_amount: Mapped[Decimal]
    max_amount: Mapped[Decimal]
    available: Mapped[Decimal]
    payment_methods: Mapped[list[str]] = mapped_column(ARRAY(String(64)))
    merchant_id: Mapped[int | None] = mapped_column(ForeignKey("merchants.id"))
    external_id: Mapped[str | None] = mapped_column(String(128))


Index(
    "ix_offer_snapshots_pair_side_time",
    OfferSnapshot.pair_id,
    OfferSnapshot.side,
    OfferSnapshot.time.desc(),
)
