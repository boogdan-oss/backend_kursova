from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collectors.base import ExchangeAdapter
from app.collectors.scheduler import CollectTask
from app.db.repositories.market import (
    get_exchange_id,
    get_or_create_pair,
    mark_fetched,
    save_market_stats,
    save_markets,
)
from app.db.repositories.offers import save_offers
from app.db.session import SessionLocal
from app.domain import MarketSnapshot, Offer, Side, SnapshotEvent, SnapshotUpdate
from app.engine.pricing import exchange_snapshots
from app.pubsub import get_redis, publish_event


class MarketStatsTracker:
    """Останні оголошення обох сторін пари.

    Задача збору приносить одну сторону, а рядку market_stats потрібні обидві.
    """

    def __init__(self) -> None:
        self._sides: dict[tuple[str, str, str], dict[Side, list[Offer]]] = {}

    def update(
        self, task: CollectTask, offers: list[Offer], fetched_at: datetime
    ) -> list[MarketSnapshot]:
        """Знімок біржі: загальний, далі по способах оплати.

        Порожньо — другу сторону ще не збирали: порожня ціна означала б «немає оголошень».
        """
        sides = self._sides.setdefault((task.exchange, task.asset, task.fiat), {})
        sides[task.side] = offers
        if len(sides) < 2:
            return []
        return exchange_snapshots(
            task.exchange, task.asset, task.fiat, sides["BUY"], sides["SELL"], fetched_at
        )

    def offers_since(self, since: datetime) -> list[Offer]:
        """Оголошення всіх бірж, зібрані не раніше since: вхід для пошуку можливостей."""
        return [
            offer
            for sides in self._sides.values()
            for offers in sides.values()
            for offer in offers
            if offer.fetched_at >= since
        ]


async def collect_task(
    adapter: ExchangeAdapter,
    task: CollectTask,
    tracker: MarketStatsTracker,
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
) -> int:
    """Один крок збору: біржа → offer_snapshots і market_stats → подія для WebSocket.

    Повертає кількість оголошень. Частоту викликів визначають rate limiter і планувальник
    (CLAUDE.md, розділ 5.1). Подія йде після коміту: клієнт, що за нею звернеться до API,
    уже побачить ці дані.
    """
    offers = await adapter.fetch_offers(task.asset, task.fiat, task.side)
    fetched_at = offers[0].fetched_at if offers else datetime.now(UTC)
    payments = sorted({method for offer in offers for method in offer.payment_methods})
    snapshots = tracker.update(task, offers, fetched_at)

    async with session_factory() as session, session.begin():
        exchange_id = await get_exchange_id(session, task.exchange)
        pair_id = await get_or_create_pair(session, task.asset, task.fiat)
        count = await save_offers(session, exchange_id, pair_id, offers)
        await mark_fetched(session, exchange_id, pair_id, task.side, fetched_at, payments)
        if snapshots:
            await save_market_stats(session, exchange_id, pair_id, snapshots[0])

    if snapshots:
        update = SnapshotUpdate(
            exchange=task.exchange, asset=task.asset, fiat=task.fiat, items=snapshots
        )
        await publish_event(get_redis(), SnapshotEvent(ts=datetime.now(UTC), data=update))
    return count


async def refresh_markets(
    adapter: ExchangeAdapter,
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
) -> int:
    """Оновлює довідник пар біржі. Повертає кількість пар."""
    markets = await adapter.list_markets()
    async with session_factory() as session, session.begin():
        return await save_markets(session, adapter.name, markets)
