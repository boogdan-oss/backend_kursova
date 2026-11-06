from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models import (
    Asset,
    CurrencyPair,
    Exchange,
    Fiat,
    MarketStats,
    Opportunity,
    SpreadHistory,
)


@dataclass(frozen=True, slots=True)
class PairRisk:
    volatility_pct: Decimal | None = None
    stability_pct: Decimal | None = None


async def save_opportunity(session: AsyncSession, values: dict[str, Any]) -> tuple[int, datetime]:
    """Записує нову можливість і повертає її ключ (id, time)."""
    result = await session.execute(
        insert(Opportunity).values(**values).returning(Opportunity.id, Opportunity.time)
    )
    row = result.one()
    return row.id, row.time


async def update_duration(
    session: AsyncSession, opportunity_id: int, time: datetime, duration_sec: int
) -> None:
    """FR-20: скільки можливість уже тримається. Умова на time — щоб не чіпати інші чанки."""
    await session.execute(
        update(Opportunity)
        .where(Opportunity.id == opportunity_id, Opportunity.time == time)
        .values(duration_sec=duration_sec)
    )


async def save_spread_history(session: AsyncSession, rows: Sequence[dict[str, Any]]) -> None:
    if rows:
        await session.execute(insert(SpreadHistory), list(rows))


async def pair_risks(
    session: AsyncSession, pair_ids: Iterable[int], since: datetime
) -> dict[int, PairRisk]:
    """Волатильність ціни й частка часу з додатним чистим спредом за період."""
    ids = list(pair_ids)
    volatility = await session.execute(
        select(
            MarketStats.pair_id,
            func.stddev_samp(MarketStats.buy_price) / func.avg(MarketStats.buy_price) * 100,
        )
        .where(MarketStats.pair_id.in_(ids), MarketStats.time >= since)
        .group_by(MarketStats.pair_id)
    )
    stability = await session.execute(
        select(
            SpreadHistory.pair_id,
            func.avg(case((SpreadHistory.net_spread > 0, 100), else_=0)),
        )
        .where(SpreadHistory.pair_id.in_(ids), SpreadHistory.time >= since)
        .group_by(SpreadHistory.pair_id)
    )
    volatility_by_pair: dict[int, Any] = {row[0]: row[1] for row in volatility.all()}
    stability_by_pair: dict[int, Any] = {row[0]: row[1] for row in stability.all()}
    return {
        pair_id: PairRisk(volatility_by_pair.get(pair_id), stability_by_pair.get(pair_id))
        for pair_id in volatility_by_pair.keys() | stability_by_pair.keys()
    }


async def opportunity_history(
    session: AsyncSession,
    asset: str | None,
    fiat: str | None,
    kind: str | None,
    since: datetime,
    limit: int,
) -> list[tuple[Opportunity, str, str, str, str]]:
    """Можливості разом із кодами активу, фіату й бірж, новіші першими."""
    buy_exchange, sell_exchange = aliased(Exchange), aliased(Exchange)
    query = (
        select(Opportunity, Asset.code, Fiat.code, buy_exchange.code, sell_exchange.code)
        .join(CurrencyPair, CurrencyPair.id == Opportunity.pair_id)
        .join(Asset, Asset.id == CurrencyPair.base_currency_id)
        .join(Fiat, Fiat.id == CurrencyPair.quote_currency_id)
        .join(buy_exchange, buy_exchange.id == Opportunity.buy_exchange_id)
        .join(sell_exchange, sell_exchange.id == Opportunity.sell_exchange_id)
        .where(Opportunity.time >= since)
        .order_by(Opportunity.time.desc())
        .limit(limit)
    )
    if asset is not None:
        query = query.where(Asset.code == asset)
    if fiat is not None:
        query = query.where(Fiat.code == fiat)
    if kind is not None:
        query = query.where(Opportunity.kind == kind)
    return [tuple(row) for row in (await session.execute(query)).all()]  # type: ignore[misc]
