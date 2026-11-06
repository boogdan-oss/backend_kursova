from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnClause, func, insert, literal_column, select, text, update
from sqlalchemy.dialects.postgresql import Insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AlertRule,
    Asset,
    CurrencyPair,
    Exchange,
    ExchangeMarket,
    ExchangeStatus,
    Fiat,
    MarketStats,
    Merchant,
    PaymentMethod,
    WatchlistItem,
)
from app.domain import MarketSnapshot, P2PMarket, Side

# Точний count(*) на hypertable дорогий; до цього розміру він усе ще миттєвий
EXACT_COUNT_MAX_ROWS = 1_000_000
HYPERTABLES = ("offer_snapshots", "market_stats")

# Об'єднання способів оплати при upsert: без дублікатів і в стабільному порядку
_MERGED_PAYMENTS: ColumnClause[Any] = literal_column(
    "ARRAY(SELECT DISTINCT unnest("
    "exchange_markets.payment_methods || excluded.payment_methods) ORDER BY 1)"
)


@dataclass(frozen=True, slots=True)
class MarketRow:
    exchange_id: int
    exchange: str
    pair_id: int
    asset: str
    fiat: str
    payment_methods: list[str]
    buy_fetched_at: datetime | None
    sell_fetched_at: datetime | None

    def fetched_at(self, side: Side) -> datetime | None:
        return self.buy_fetched_at if side == "BUY" else self.sell_fetched_at


@dataclass(frozen=True, slots=True)
class HistoryPoint:
    time: datetime
    buy_price: Any
    sell_price: Any
    spread: Any
    volume: Any
    samples: int


@dataclass(frozen=True, slots=True)
class ExchangeOverview:
    exchange: str
    status: str
    last_ok_at: datetime | None
    pairs_total: int
    pairs_collected: int


@dataclass(frozen=True, slots=True)
class TableStats:
    table: str
    rows: int
    exact: bool  # False — оцінка TimescaleDB
    size_mb: float


async def _get_or_create_code(session: AsyncSession, model: type[Asset | Fiat], code: str) -> int:
    """Нові активи й фіати з'являються динамічно; назву згодом уточнить довідник бірж."""
    query = select(model.id).where(model.code == code)
    existing = (await session.execute(query)).scalar_one_or_none()
    if existing is not None:
        return existing
    await session.execute(
        pg_insert(model)
        .values(code=code, name=code)
        .on_conflict_do_nothing(index_elements=["code"])
    )
    return (await session.execute(query)).scalar_one()


async def get_or_create_pair(session: AsyncSession, asset: str, fiat: str) -> int:
    asset_id = await _get_or_create_code(session, Asset, asset)
    fiat_id = await _get_or_create_code(session, Fiat, fiat)
    query = select(CurrencyPair.id).where(
        CurrencyPair.base_currency_id == asset_id, CurrencyPair.quote_currency_id == fiat_id
    )
    existing = (await session.execute(query)).scalar_one_or_none()
    if existing is not None:
        return existing
    await session.execute(
        pg_insert(CurrencyPair)
        .values(base_currency_id=asset_id, quote_currency_id=fiat_id)
        .on_conflict_do_nothing(index_elements=["base_currency_id", "quote_currency_id"])
    )
    return (await session.execute(query)).scalar_one()


async def get_exchange_id(session: AsyncSession, exchange: str) -> int:
    query = select(Exchange.id).where(Exchange.code == exchange)
    return (await session.execute(query)).scalar_one()


def _upsert_market(values: dict[str, Any], updates: dict[str, Any]) -> Insert:
    return (
        pg_insert(ExchangeMarket)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["exchange_id", "pair_id"],
            set_={**updates, "payment_methods": _MERGED_PAYMENTS},
        )
    )


async def save_markets(session: AsyncSession, exchange: str, markets: Sequence[P2PMarket]) -> int:
    """Оновлює довідник із `list_markets`. Коміт — на стороні сервісу."""
    exchange_id = await get_exchange_id(session, exchange)
    listed_at = datetime.now(UTC)
    for market in markets:
        pair_id = await get_or_create_pair(session, market.asset, market.fiat)
        await session.execute(
            _upsert_market(
                {
                    "exchange_id": exchange_id,
                    "pair_id": pair_id,
                    "payment_methods": market.payment_methods,
                    "listed_at": listed_at,
                },
                {"listed_at": listed_at},
            )
        )

    codes = sorted({code for market in markets for code in market.payment_methods})
    if codes:
        await session.execute(
            pg_insert(PaymentMethod)
            .values([{"code": code, "name": code} for code in codes])
            .on_conflict_do_nothing(index_elements=["code"])
        )
    return len(markets)


async def mark_fetched(
    session: AsyncSession,
    exchange_id: int,
    pair_id: int,
    side: Side,
    fetched_at: datetime,
    payment_methods: list[str],
) -> None:
    """Пересуває вказівник на останній пакет сторони — разом із записом самого пакета."""
    column = "buy_fetched_at" if side == "BUY" else "sell_fetched_at"
    await session.execute(
        _upsert_market(
            {
                "exchange_id": exchange_id,
                "pair_id": pair_id,
                "payment_methods": payment_methods,
                column: fetched_at,
            },
            {column: fetched_at},
        )
    )
    await session.execute(
        update(Exchange).where(Exchange.id == exchange_id).values(last_ok_at=fetched_at)
    )


async def save_market_stats(
    session: AsyncSession, exchange_id: int, pair_id: int, snapshot: MarketSnapshot
) -> None:
    await session.execute(
        insert(MarketStats).values(
            time=snapshot.fetched_at,
            exchange_id=exchange_id,
            pair_id=pair_id,
            buy_price=snapshot.buy_price,
            sell_price=snapshot.sell_price,
            spread=snapshot.spread_pct,
            volume=snapshot.volume,
            offers_count_buy=snapshot.offers_count_buy,
            offers_count_sell=snapshot.offers_count_sell,
        )
    )


async def find_markets(
    session: AsyncSession,
    exchange: str | None = None,
    asset: str | None = None,
    fiat: str | None = None,
) -> list[MarketRow]:
    query = (
        select(
            ExchangeMarket.exchange_id,
            Exchange.code,
            ExchangeMarket.pair_id,
            Asset.code,
            Fiat.code,
            ExchangeMarket.payment_methods,
            ExchangeMarket.buy_fetched_at,
            ExchangeMarket.sell_fetched_at,
        )
        .join(Exchange, Exchange.id == ExchangeMarket.exchange_id)
        .join(CurrencyPair, CurrencyPair.id == ExchangeMarket.pair_id)
        .join(Asset, Asset.id == CurrencyPair.base_currency_id)
        .join(Fiat, Fiat.id == CurrencyPair.quote_currency_id)
        .order_by(Exchange.code, Asset.code, Fiat.code)
    )
    if exchange is not None:
        query = query.where(Exchange.code == exchange)
    if asset is not None:
        query = query.where(Asset.code == asset)
    if fiat is not None:
        query = query.where(Fiat.code == fiat)
    return [MarketRow(*row) for row in (await session.execute(query)).all()]


async def markets_listed_at(session: AsyncSession, exchange: str) -> datetime | None:
    """Коли довідник біржі оновлювали востаннє."""
    query = (
        select(func.max(ExchangeMarket.listed_at))
        .join(Exchange, Exchange.id == ExchangeMarket.exchange_id)
        .where(Exchange.code == exchange)
    )
    return (await session.execute(query)).scalar_one()


async def tracked_pairs(session: AsyncSession) -> set[tuple[str, str]]:
    """Пари з watchlist і активних alert_rules — вони оновлюються найчастіше (FR-17)."""
    pair_ids = select(WatchlistItem.pair_id).union(
        select(AlertRule.pair_id).where(AlertRule.enabled)
    )
    query = (
        select(Asset.code, Fiat.code)
        .select_from(CurrencyPair)
        .join(Asset, Asset.id == CurrencyPair.base_currency_id)
        .join(Fiat, Fiat.id == CurrencyPair.quote_currency_id)
        .where(CurrencyPair.id.in_(pair_ids))
    )
    return {(asset, fiat) for asset, fiat in (await session.execute(query)).all()}


async def set_exchange_status(session: AsyncSession, exchange: str, status: ExchangeStatus) -> None:
    await session.execute(update(Exchange).where(Exchange.code == exchange).values(status=status))


async def market_history(
    session: AsyncSession,
    exchange_id: int,
    pair_id: int,
    start: datetime,
    end: datetime,
    interval: timedelta,
) -> list[HistoryPoint]:
    bucket = func.time_bucket(interval, MarketStats.time).label("bucket")
    query = (
        select(
            bucket,
            func.avg(MarketStats.buy_price),
            func.avg(MarketStats.sell_price),
            func.avg(MarketStats.spread),
            func.avg(MarketStats.volume),
            func.count(),
        )
        .where(
            MarketStats.exchange_id == exchange_id,
            MarketStats.pair_id == pair_id,
            MarketStats.payment_method_id.is_(None),
            MarketStats.time >= start,
            MarketStats.time < end,
        )
        # За псевдонімом: інакше Postgres бачить два різні параметри interval
        .group_by(text("bucket"))
        .order_by(text("bucket"))
    )
    return [HistoryPoint(*row) for row in (await session.execute(query)).all()]


async def exchange_overview(
    session: AsyncSession, exchanges: Iterable[str]
) -> list[ExchangeOverview]:
    collected = (ExchangeMarket.buy_fetched_at.is_not(None)) | (
        ExchangeMarket.sell_fetched_at.is_not(None)
    )
    query = (
        select(
            Exchange.code,
            Exchange.status,
            Exchange.last_ok_at,
            func.count(ExchangeMarket.pair_id),
            func.count(ExchangeMarket.pair_id).filter(collected),
        )
        .outerjoin(ExchangeMarket, ExchangeMarket.exchange_id == Exchange.id)
        .where(Exchange.code.in_(list(exchanges)))
        .group_by(Exchange.id)
        .order_by(Exchange.code)
    )
    return [
        ExchangeOverview(code, status.value, last_ok_at, total, fetched)
        for code, status, last_ok_at, total, fetched in (await session.execute(query)).all()
    ]


async def merchants_count(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(Merchant))).scalar_one()


async def table_stats(session: AsyncSession) -> list[TableStats]:
    """Обсяг hypertables. Імена таблиць — лише з константи HYPERTABLES."""
    result = []
    for table in HYPERTABLES:
        estimate = (await session.execute(select(func.approximate_row_count(table)))).scalar_one()
        size = (await session.execute(select(func.hypertable_size(table)))).scalar_one()
        exact = estimate < EXACT_COUNT_MAX_ROWS
        rows = estimate
        if exact:
            rows = (await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
        result.append(TableStats(table, rows, exact, round((size or 0) / 1024 / 1024, 2)))
    return result
