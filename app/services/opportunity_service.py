import asyncio
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collectors.payment_map import normalize_payment
from app.collectors.registry import exchange_fees
from app.db.repositories.market import MarketRow, find_markets
from app.db.repositories.offers import fresh_offers
from app.db.repositories.opportunities import (
    opportunity_history,
    pair_risks,
    save_opportunity,
    save_spread_history,
    update_duration,
)
from app.db.session import SessionLocal
from app.domain import (
    Deal,
    FeeSchedule,
    MarketSnapshot,
    Offer,
    Opportunity,
    OpportunityKind,
    Side,
    Step,
)
from app.engine.arbitrage import (
    DEFAULT_ADVERSE_MOVE_PCT,
    MarketRisk,
    PairKey,
    evaluate_opportunities,
    find_chains,
    find_opportunities,
    is_actionable,
)
from app.engine.fees import calculate_deal
from app.engine.filters import filter_by_payment, filter_reliable
from app.engine.pricing import (
    Fill,
    effective_price,
    fill_order,
    market_snapshot,
    quantity_for,
    typical_amount,
)
from app.engine.ranking import OpportunityFilter, filter_opportunities
from app.engine.scoring import potential_loss, risk_reward
from app.engine.spreads import PERCENT_PRECISION, round_money
from app.services.collect_service import MarketStatsTracker
from app.services.market_service import PairNotFound

OFFERS_MAX_AGE = timedelta(minutes=5)
RISK_WINDOW = timedelta(hours=1)

OpportunityKey = tuple[OpportunityKind, str, str, str, str]

log = structlog.get_logger()


class PriceUnavailable(Exception):
    """Немає свіжих оголошень, щоб порахувати угоду."""


@dataclass(frozen=True, slots=True)
class DealQuote:
    deal: Deal
    potential_loss: Decimal
    rr: Decimal | None
    buy_fill: Fill | None
    sell_fill: Fill | None


@dataclass(frozen=True, slots=True)
class OpportunityRecord:
    opportunity: Opportunity
    duration_sec: int | None


async def _load_risks(
    session: AsyncSession, markets: Sequence[MarketRow], now: datetime
) -> dict[PairKey, MarketRisk]:
    pairs = {market.pair_id: (market.asset, market.fiat) for market in markets}
    risks = await pair_risks(session, pairs, now - RISK_WINDOW)
    return {
        pairs[pair_id]: MarketRisk(risk.stability_pct, risk.volatility_pct)
        for pair_id, risk in risks.items()
    }


def _snapshots(offers: Iterable[Offer], now: datetime) -> list[MarketSnapshot]:
    """Котирування для графа ланцюжків. Лише надійні продавці: у кількості й обсязі теж."""
    sides: dict[tuple[str, str, str], dict[Side, list[Offer]]] = defaultdict(
        lambda: {"BUY": [], "SELL": []}
    )
    for offer in offers:
        sides[offer.exchange, offer.asset, offer.fiat][offer.side].append(offer)
    return [
        market_snapshot(
            exchange,
            asset,
            fiat,
            filter_reliable(side["BUY"]),
            filter_reliable(side["SELL"]),
            now,
        )
        for (exchange, asset, fiat), side in sides.items()
    ]


def _chains_for(
    offers: list[Offer],
    fees: dict[str, FeeSchedule],
    fiat: str,
    investment: Decimal | None,
    now: datetime,
) -> list[Opportunity]:
    amount = investment or typical_amount(
        filter_reliable([offer for offer in offers if offer.fiat == fiat])
    )
    if amount is None:
        return []
    return find_chains(_snapshots(offers, now), fees, fiat, amount, now)


async def get_opportunities(
    session: AsyncSession,
    asset: str | None,
    fiat: str | None,
    criteria: OpportunityFilter,
    investment: Decimal | None = None,
) -> list[Opportunity]:
    """Поточні можливості зі свіжих оголошень у БД, найкращі першими.

    Ланцюжки лише на окремий запит: їм потрібні оголошення всіх пар, а не однієї.
    """
    now = datetime.now(UTC)
    since = now - OFFERS_MAX_AGE
    if criteria.kind == "chain":
        if fiat is None:
            raise ValueError("Для ланцюжків потрібен fiat: з нього цикл починається")
        markets = await find_markets(session)
        offers = await fresh_offers(session, markets, since)
        found = await asyncio.to_thread(_chains_for, offers, exchange_fees(), fiat, investment, now)
        return filter_opportunities(found, criteria)

    markets = await find_markets(session, asset=asset, fiat=fiat)
    if asset is not None and fiat is not None and not markets:
        raise PairNotFound(asset, fiat)
    offers = await fresh_offers(session, markets, since)
    risks = await _load_risks(session, markets, now)
    found = find_opportunities(offers, exchange_fees(), now, investment, risks)
    return filter_opportunities(found, criteria)


async def get_opportunity_history(
    session: AsyncSession,
    asset: str | None,
    fiat: str | None,
    kind: str | None,
    hours: int,
    limit: int,
) -> list[OpportunityRecord]:
    since = datetime.now(UTC) - timedelta(hours=hours)
    rows = await opportunity_history(session, asset, fiat, kind, since, limit)
    return [
        OpportunityRecord(
            Opportunity(
                kind=row.kind,
                asset=asset_code,
                fiat=fiat_code,
                buy_exchange=buy_exchange,
                sell_exchange=sell_exchange,
                buy_price=row.buy_price,
                sell_price=row.sell_price,
                steps=[Step.model_validate(step) for step in row.steps],
                spread_pct=row.spread,
                net_spread_pct=row.net_spread,
                estimated_profit=row.estimated_profit,
                max_volume=row.max_volume,
                rr=row.rr,
                risk_score=row.risk_score,
                risk_level=row.risk_level,
                ai_score=row.ai_score,
                detected_at=row.time,
            ),
            row.duration_sec,
        )
        for row, asset_code, fiat_code, buy_exchange, sell_exchange in rows
    ]


async def _market_fill(
    session: AsyncSession,
    exchange: str,
    asset: str,
    fiat: str,
    side: Side,
    quantity: Decimal | None,
    investment: Decimal,
    payment: str | None,
) -> Fill:
    """Ціна з глибини ринку: рівні надійних продавців для потрібної кількості."""
    markets = await find_markets(session, exchange, asset, fiat)
    if not markets:
        raise PairNotFound(asset, fiat)
    since = datetime.now(UTC) - OFFERS_MAX_AGE
    offers = [offer for offer in await fresh_offers(session, markets, since) if offer.side == side]
    offers = filter_reliable(offers)
    if payment is not None:
        offers = filter_by_payment(offers, normalize_payment(payment))

    if quantity is None:
        reference = effective_price(offers)
        if reference is None:
            raise PriceUnavailable(f"{exchange}: немає свіжих оголошень {asset}/{fiat} ({side})")
        quantity = quantity_for(investment, reference)
    fill = fill_order(offers, quantity)
    if fill is None:
        raise PriceUnavailable(f"{exchange}: немає оголошень {asset}/{fiat} на цю суму ({side})")
    return fill


async def calculate(
    session: AsyncSession,
    asset: str,
    fiat: str,
    investment: Decimal,
    buy_exchange: str | None = None,
    sell_exchange: str | None = None,
    buy_price: Decimal | None = None,
    sell_price: Decimal | None = None,
    extra_fees: Decimal = Decimal(0),
    adverse_move_pct: Decimal = DEFAULT_ADVERSE_MOVE_PCT,
    payment: str | None = None,
) -> DealQuote:
    """Симулятор угоди (FR-08, FR-18). Без цін бере їх із глибини ринку разом зі slippage."""
    buy_fill = sell_fill = None
    if buy_price is None:
        assert buy_exchange is not None
        buy_fill = await _market_fill(
            session, buy_exchange, asset, fiat, "BUY", None, investment, payment
        )
        buy_price = buy_fill.average_price
    if sell_price is None:
        assert sell_exchange is not None
        sell_fill = await _market_fill(
            session,
            sell_exchange,
            asset,
            fiat,
            "SELL",
            quantity_for(investment, buy_price),
            investment,
            payment,
        )
        sell_price = sell_fill.average_price

    fees = exchange_fees()
    no_fees = FeeSchedule(exchange="manual")
    buy_fees = fees[buy_exchange] if buy_exchange else no_fees
    sell_fees = fees[sell_exchange] if sell_exchange else no_fees
    is_transfer = bool(buy_exchange and sell_exchange and buy_exchange != sell_exchange)
    deal = calculate_deal(
        investment,
        buy_price,
        sell_price,
        buy_fees,
        sell_fees,
        transfer_fee=buy_fees.withdrawal_fee(asset) if is_transfer else Decimal(0),
        extra_fees=extra_fees,
    )
    loss = round_money(potential_loss(deal.fees, investment, adverse_move_pct))
    rr = risk_reward(deal.net_profit, loss)
    rounded_rr = None if rr is None else rr.quantize(PERCENT_PRECISION)
    return DealQuote(deal, loss, rounded_rr, buy_fill, sell_fill)


def _opportunity_values(
    item: Opportunity, exchange_ids: dict[str, int], pair_id: int
) -> dict[str, Any]:
    return {
        "time": item.detected_at,
        "kind": item.kind,
        "pair_id": pair_id,
        "buy_exchange_id": exchange_ids[item.buy_exchange],
        "sell_exchange_id": exchange_ids[item.sell_exchange],
        "buy_price": item.buy_price,
        "sell_price": item.sell_price,
        "steps": [step.model_dump(mode="json") for step in item.steps],
        "spread": item.spread_pct,
        "net_spread": item.net_spread_pct,
        "estimated_profit": item.estimated_profit,
        "max_volume": item.max_volume,
        "rr": item.rr,
        "ai_score": item.ai_score,
        "risk_score": item.risk_score,
        "risk_level": item.risk_level,
        "duration_sec": 0,
    }


class OpportunityScanner:
    """Фоновий пошук можливостей у collector: історія спреду й тривалість можливостей.

    Можливість пишеться в БД один раз, коли з'явилась; далі оновлюється лише її тривалість
    (FR-19, FR-20).
    """

    def __init__(
        self,
        tracker: MarketStatsTracker,
        priority_pairs: set[PairKey],
        session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
    ) -> None:
        self._tracker = tracker
        self._chain_fiats = {fiat for _, fiat in priority_pairs}
        self._session_factory = session_factory
        self._open: dict[OpportunityKey, tuple[int, datetime]] = {}

    def active_pairs(self) -> set[PairKey]:
        """Пари з поточними міжбіржовими можливостями: вони мають оновлюватись найчастіше."""
        return {(key[1], key[2]) for key in self._open if key[0] == "cross_exchange"}

    async def scan(self) -> None:
        now = datetime.now(UTC)
        offers = self._tracker.offers_since(now - OFFERS_MAX_AGE)
        if not offers:
            return

        fees = exchange_fees()
        async with self._session_factory() as session, session.begin():
            markets = await find_markets(session)
            pair_ids = {(market.asset, market.fiat): market.pair_id for market in markets}
            exchange_ids = {market.exchange: market.exchange_id for market in markets}
            risks = await _load_risks(session, markets, now)
            evaluated = await asyncio.to_thread(
                evaluate_opportunities, offers, fees, now, None, risks
            )
            found = [item for item in evaluated if is_actionable(item)]
            for fiat in self._chain_fiats:
                found += await asyncio.to_thread(_chains_for, offers, fees, fiat, None, now)

            await save_spread_history(
                session, self._spread_rows(evaluated, pair_ids, exchange_ids, now)
            )
            await self._sync_opportunities(session, found, pair_ids, exchange_ids, now)
        log.info("collector.opportunities", found=len(found), open=len(self._open))

    @staticmethod
    def _spread_rows(
        evaluated: list[Opportunity],
        pair_ids: dict[PairKey, int],
        exchange_ids: dict[str, int],
        now: datetime,
    ) -> list[dict[str, Any]]:
        """Найкращий міжбіржовий напрямок кожної пари, навіть збитковий: це історія спреду."""
        best: dict[PairKey, Opportunity] = {}
        for item in evaluated:
            if item.kind != "cross_exchange":
                continue
            key = (item.asset, item.fiat)
            if key not in best or item.net_spread_pct > best[key].net_spread_pct:
                best[key] = item
        return [
            {
                "time": now,
                "pair_id": pair_ids[key],
                "buy_exchange_id": exchange_ids[item.buy_exchange],
                "sell_exchange_id": exchange_ids[item.sell_exchange],
                "buy_price": item.buy_price,
                "sell_price": item.sell_price,
                "spread": item.spread_pct,
                "net_spread": item.net_spread_pct,
            }
            for key, item in best.items()
            if key in pair_ids
        ]

    async def _sync_opportunities(
        self,
        session: AsyncSession,
        found: list[Opportunity],
        pair_ids: dict[PairKey, int],
        exchange_ids: dict[str, int],
        now: datetime,
    ) -> None:
        current: dict[OpportunityKey, Opportunity] = {}
        for item in found:
            key = (item.kind, item.asset, item.fiat, item.buy_exchange, item.sell_exchange)
            if key not in current or item.net_spread_pct > current[key].net_spread_pct:
                current[key] = item

        for key, item in current.items():
            if key in self._open:
                row_id, started_at = self._open[key]
                duration = int((now - started_at).total_seconds())
                await update_duration(session, row_id, started_at, duration)
            elif (item.asset, item.fiat) in pair_ids:
                values = _opportunity_values(item, exchange_ids, pair_ids[item.asset, item.fiat])
                self._open[key] = await save_opportunity(session, values)
        self._open = {key: value for key, value in self._open.items() if key in current}
