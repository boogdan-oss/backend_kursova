"""Бюджет місця на справжній TimescaleDB: видаляються найстаріші дні, поточний лишається."""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import MarketStats, OfferSnapshot
from app.db.repositories.market import drop_oldest_offer_chunk, storage_bytes
from app.services.storage_service import StorageGuard

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL не заданий")


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE offer_snapshots, market_stats CASCADE"))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def write_days(session_factory: async_sessionmaker[AsyncSession], days: int) -> None:
    """По рядку на кожен із останніх днів: у TimescaleDB це окремі чанки."""
    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        ids = await session.execute(
            text(
                "SELECT (SELECT id FROM exchanges LIMIT 1), (SELECT id FROM currency_pairs LIMIT 1)"
            )
        )
        exchange_id, pair_id = ids.one()
        for day in range(days):
            time = now - timedelta(days=day)
            for model, values in (
                (
                    OfferSnapshot,
                    {
                        "side": "BUY",
                        "price": 41,
                        "min_amount": 1,
                        "max_amount": 2,
                        "available": 3,
                        "payment_methods": [],
                    },
                ),
                (MarketStats, {"volume": 1, "offers_count_buy": 1, "offers_count_sell": 1}),
            ):
                await session.execute(
                    model.__table__.insert().values(
                        time=time, exchange_id=exchange_id, pair_id=pair_id, **values
                    )
                )


async def offer_days(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as session:
        return (await session.execute(select(func.count()).select_from(OfferSnapshot))).scalar_one()


async def test_drop_oldest_keeps_current_day(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await write_days(session_factory, 3)

    async with session_factory() as session, session.begin():
        first = await drop_oldest_offer_chunk(session)
        second = await drop_oldest_offer_chunk(session)
        third = await drop_oldest_offer_chunk(session)

    assert first is not None and second is not None and first < second
    assert third is None
    assert await offer_days(session_factory) == 1


async def test_guard_trims_old_days_until_within_budget(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await write_days(session_factory, 4)
    async with session_factory() as session:
        used = await storage_bytes(session)

    guard = StorageGuard(budget_bytes=used - 1, session_factory=session_factory)
    await guard.enforce()

    assert await offer_days(session_factory) < 4
    async with session_factory() as session:
        market_stats_rows = (
            await session.execute(select(func.count()).select_from(MarketStats))
        ).scalar_one()
    assert market_stats_rows == 4


async def test_guard_pauses_when_only_current_day_is_left(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await write_days(session_factory, 2)

    guard = StorageGuard(budget_bytes=1, session_factory=session_factory)
    await guard.enforce()

    assert guard.full is True
    assert await offer_days(session_factory) == 1


async def test_guard_within_budget_changes_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await write_days(session_factory, 3)

    guard = StorageGuard(budget_bytes=10**12, session_factory=session_factory)
    await guard.enforce()

    assert guard.full is False
    assert guard.used_bytes is not None and guard.used_bytes > 0
    assert await offer_days(session_factory) == 3


async def test_guard_trims_when_disk_is_low_within_budget(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await write_days(session_factory, 4)
    free = iter([1, 1, 10**12])

    guard = StorageGuard(
        budget_bytes=10**12,
        session_factory=session_factory,
        min_free_bytes=100,
        disk_free=lambda: next(free, 10**12),
    )
    await guard.enforce()

    assert await offer_days(session_factory) == 2
    assert guard.full is False


async def test_guard_pauses_when_disk_stays_low(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await write_days(session_factory, 3)

    guard = StorageGuard(
        budget_bytes=10**12,
        session_factory=session_factory,
        min_free_bytes=100,
        disk_free=lambda: 1,
    )
    await guard.enforce()

    assert guard.full is True
    assert guard.free_bytes == 1
    assert await offer_days(session_factory) == 1
