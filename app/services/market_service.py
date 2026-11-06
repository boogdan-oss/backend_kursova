from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.payment_map import normalize_payment
from app.collectors.registry import available_exchanges
from app.core.config import get_settings
from app.db.repositories.market import (
    ExchangeOverview,
    HistoryPoint,
    MarketRow,
    TableStats,
    exchange_overview,
    find_markets,
    market_history,
    merchants_count,
    table_stats,
)
from app.db.repositories.offers import latest_offers
from app.domain import CollectorStatus, Freshness, MarketSnapshot, Offer, Side
from app.engine.filters import filter_by_amount, filter_by_payment
from app.engine.pricing import market_snapshot
from app.pubsub import get_redis
from app.services.collector_state import read_status, request_pair

SIDES: tuple[Side, Side] = ("BUY", "SELL")


class PairNotFound(Exception):
    """Пари немає в довіднику жодної біржі."""

    def __init__(self, asset: str, fiat: str) -> None:
        super().__init__(f"{asset}/{fiat}")
        self.asset = asset
        self.fiat = fiat


@dataclass(frozen=True, slots=True)
class OffersView:
    offers: list[Offer]
    freshness: list[Freshness]
    queued: bool  # collector отримав запит оновити пару


@dataclass(frozen=True, slots=True)
class SnapshotView:
    items: list[MarketSnapshot]
    freshness: list[Freshness]
    queued: bool


@dataclass(frozen=True, slots=True)
class ExchangeStatusView:
    overview: ExchangeOverview
    collector: CollectorStatus | None  # None — collector не працює


@dataclass(frozen=True, slots=True)
class CollectorStatusView:
    exchanges: list[ExchangeStatusView]
    tables: list[TableStats]
    merchants: int


def build_freshness(market: MarketRow, sides: tuple[Side, ...], now: datetime) -> Freshness:
    times = [market.fetched_at(side) for side in sides]
    known = [time for time in times if time is not None]
    age_sec = None
    if len(known) == len(sides):
        age_sec = round((now - min(known)).total_seconds(), 1)
    return Freshness(
        exchange=market.exchange,
        buy_fetched_at=market.buy_fetched_at,
        sell_fetched_at=market.sell_fetched_at,
        age_sec=age_sec,
        stale=age_sec is None or age_sec > get_settings().data_stale_after_sec,
    )


async def _load_markets(
    session: AsyncSession, asset: str, fiat: str, exchange: str | None
) -> list[MarketRow]:
    markets = await find_markets(session, exchange, asset, fiat)
    if not markets:
        raise PairNotFound(asset, fiat)
    return markets


async def _queue_if_stale(asset: str, fiat: str, freshness: list[Freshness]) -> bool:
    """Сценарій 13.4: якщо дані застарілі, collector піднімає пріоритет пари."""
    if not any(item.stale for item in freshness):
        return False
    return await request_pair(get_redis(), asset, fiat)


async def get_offers(
    session: AsyncSession,
    asset: str,
    fiat: str,
    side: Side,
    exchange: str | None = None,
    payment: str | None = None,
    amount: Decimal | None = None,
) -> OffersView:
    """Оголошення з останнього збору. API не ходить до бірж: дані збирає collector."""
    markets = await _load_markets(session, asset, fiat, exchange)
    offers: list[Offer] = []
    for market in markets:
        offers.extend(await latest_offers(session, market, side))
    if payment is not None:
        offers = filter_by_payment(offers, normalize_payment(payment))
    if amount is not None:
        offers = filter_by_amount(offers, amount)
    offers.sort(key=lambda offer: offer.price, reverse=side == "SELL")

    now = datetime.now(UTC)
    freshness = [build_freshness(market, (side,), now) for market in markets]
    return OffersView(offers, freshness, await _queue_if_stale(asset, fiat, freshness))


async def get_snapshot(
    session: AsyncSession,
    asset: str,
    fiat: str,
    exchange: str | None = None,
    amount: Decimal | None = None,
) -> SnapshotView:
    """Ефективні ціни по біржах: спершу загальна, далі — по кожному способу оплати."""
    markets = await _load_markets(session, asset, fiat, exchange)
    items: list[MarketSnapshot] = []
    for market in markets:
        times = [time for time in map(market.fetched_at, SIDES) if time is not None]
        if not times:
            continue
        buy = await latest_offers(session, market, "BUY")
        sell = await latest_offers(session, market, "SELL")
        if amount is not None:
            buy, sell = filter_by_amount(buy, amount), filter_by_amount(sell, amount)
        fetched_at = max(times)
        items.append(market_snapshot(market.exchange, asset, fiat, buy, sell, fetched_at))
        for payment in sorted({key for offer in [*buy, *sell] for key in offer.payment_methods}):
            items.append(
                market_snapshot(
                    market.exchange,
                    asset,
                    fiat,
                    filter_by_payment(buy, payment),
                    filter_by_payment(sell, payment),
                    fetched_at,
                    payment_method=payment,
                )
            )

    now = datetime.now(UTC)
    freshness = [build_freshness(market, SIDES, now) for market in markets]
    return SnapshotView(items, freshness, await _queue_if_stale(asset, fiat, freshness))


async def get_history(
    session: AsyncSession,
    exchange: str,
    asset: str,
    fiat: str,
    start: datetime,
    end: datetime,
    interval: timedelta,
) -> list[HistoryPoint]:
    markets = await _load_markets(session, asset, fiat, exchange)
    return await market_history(
        session, markets[0].exchange_id, markets[0].pair_id, start, end, interval
    )


async def get_collector_status(session: AsyncSession) -> CollectorStatusView:
    redis = get_redis()
    exchanges = [
        ExchangeStatusView(overview, await read_status(redis, overview.exchange))
        for overview in await exchange_overview(session, available_exchanges())
    ]
    return CollectorStatusView(
        exchanges, await table_stats(session), await merchants_count(session)
    )
