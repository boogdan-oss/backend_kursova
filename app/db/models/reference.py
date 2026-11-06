"""Довідники: біржі, активи, фіати, способи оплати, валютні пари.

Наповнюються динамічно з `ExchangeAdapter.list_markets()` (Celery Beat раз на добу).
"""

from datetime import datetime

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, str_enum
from app.db.models.enums import ExchangeStatus


class Exchange(Base):
    __tablename__ = "exchanges"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)  # "binance"
    name: Mapped[str] = mapped_column(String(64))  # "Binance P2P"
    api_url: Mapped[str] = mapped_column(String(255))
    status: Mapped[ExchangeStatus] = mapped_column(
        str_enum(ExchangeStatus), default=ExchangeStatus.ACTIVE
    )
    last_ok_at: Mapped[datetime | None]  # останній успішний запит (NFR-05, /health)


class Asset(Base):
    """Крипто-актив: тікер біржі у верхньому регістрі (USDT, BTC, ...)."""

    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True)
    name: Mapped[str] = mapped_column(String(64))


class Fiat(Base):
    """Фіатна валюта за ISO 4217 (UAH, EUR, ...)."""

    __tablename__ = "fiats"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(8), unique=True)
    name: Mapped[str] = mapped_column(String(64))


class PaymentMethod(Base):
    """Нормалізований спосіб оплати: "monobank", "revolut", "sepa", ..."""

    __tablename__ = "payment_methods"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    # NULL — спосіб оплати не прив'язаний до однієї валюти (напр. Revolut)
    fiat_id: Mapped[int | None] = mapped_column(ForeignKey("fiats.id", ondelete="SET NULL"))

    aliases: Mapped[list["PaymentMethodAlias"]] = relationship(
        back_populates="payment_method", cascade="all, delete-orphan", lazy="selectin"
    )


class PaymentMethodAlias(Base):
    """Назва способу оплати на конкретній біржі ("Mono", "MonoBank") → нормалізований ключ."""

    __tablename__ = "payment_method_aliases"
    __table_args__ = (UniqueConstraint("exchange_id", "alias"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange_id: Mapped[int] = mapped_column(ForeignKey("exchanges.id", ondelete="CASCADE"))
    payment_method_id: Mapped[int] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="CASCADE")
    )
    alias: Mapped[str] = mapped_column(String(128))

    payment_method: Mapped[PaymentMethod] = relationship(back_populates="aliases")


class CurrencyPair(Base):
    """Валютна пара P2P: base_currency — крипто-актив, quote_currency — фіат (USDT/UAH)."""

    __tablename__ = "currency_pairs"
    __table_args__ = (UniqueConstraint("base_currency_id", "quote_currency_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    base_currency_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    quote_currency_id: Mapped[int] = mapped_column(ForeignKey("fiats.id", ondelete="RESTRICT"))

    base_currency: Mapped[Asset] = relationship(lazy="joined")
    quote_currency: Mapped[Fiat] = relationship(lazy="joined")


class ExchangeMarket(Base):
    """Пара на конкретній біржі: що підтримує біржа і наскільки свіжі дані.

    buy_fetched_at / sell_fetched_at — час останнього збору сторони. Він же вказує на актуальний
    пакет в offer_snapshots: API не шукає max(time) по hypertable, а порожня сторона не показує
    старі оголошення.
    """

    __tablename__ = "exchange_markets"

    exchange_id: Mapped[int] = mapped_column(
        ForeignKey("exchanges.id", ondelete="CASCADE"), primary_key=True
    )
    pair_id: Mapped[int] = mapped_column(
        ForeignKey("currency_pairs.id", ondelete="CASCADE"), primary_key=True
    )
    # З довідника біржі й реальних оголошень: довідник перелічує лише частину способів оплати
    payment_methods: Mapped[list[str]] = mapped_column(ARRAY(String(64)))
    listed_at: Mapped[datetime | None]  # NULL — пари ще не було в довіднику біржі
    buy_fetched_at: Mapped[datetime | None]
    sell_fetched_at: Mapped[datetime | None]
