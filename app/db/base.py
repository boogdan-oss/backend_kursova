from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, Enum, MetaData, Numeric, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Стабільні імена обмежень — щоб Alembic autogenerate давав передбачувані міграції
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Гроші й ціни (P2P ціна BTC у фіаті може бути дуже великою, обсяг — дуже малим)
Money = Numeric(28, 8)
# Відсотки (Spread, Net Spread, ROI) і відношення (RR)
Percent = Numeric(12, 4)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {
        datetime: DateTime(timezone=True),
        Decimal: Money,
    }


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


def str_enum(enum_cls: type[StrEnum], length: int = 16) -> Enum:
    """VARCHAR + CHECK замість нативного ENUM Postgres — простіше змінювати в міграціях."""
    return Enum(
        enum_cls,
        native_enum=False,
        create_constraint=True,
        length=length,
        values_callable=lambda cls: [member.value for member in cls],
        validate_strings=True,
    )


def hypertable(time_column: str = "time") -> dict[str, Any]:
    """Позначка для міграцій: таблицю треба перетворити на TimescaleDB hypertable.

    TimescaleDB вимагає, щоб первинний ключ і унікальні індекси містили колонку часу,
    тому в hypertables PK складений: (id, time). Зовнішні ключі НА hypertable не ставимо.
    """
    return {"info": {"hypertable": {"time_column": time_column}}}
