from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, Identity, Index, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, hypertable, str_enum
from app.db.models.enums import PredictionTarget


class Prediction(Base):
    """Прогноз моделі — зберігається, щоб потім порівняти з фактом (MAE, MAPE)."""

    __tablename__ = "predictions"
    __table_args__ = hypertable()

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    time: Mapped[datetime] = mapped_column(primary_key=True)  # коли зроблено прогноз
    model_version: Mapped[str] = mapped_column(String(64))
    pair_id: Mapped[int] = mapped_column(ForeignKey("currency_pairs.id"))
    # NULL — прогноз по ринку пари загалом, а не по одній біржі
    exchange_id: Mapped[int | None] = mapped_column(ForeignKey("exchanges.id"))
    target: Mapped[PredictionTarget] = mapped_column(str_enum(PredictionTarget))
    horizon: Mapped[int] = mapped_column(SmallInteger)  # хвилини: 15–60
    predicted: Mapped[Decimal]
    predicted_lower: Mapped[Decimal | None]  # інтервал довіри
    predicted_upper: Mapped[Decimal | None]
    actual: Mapped[Decimal | None]  # заповнюється, коли настає time + horizon


Index("ix_predictions_pair_time", Prediction.pair_id, Prediction.time.desc())
