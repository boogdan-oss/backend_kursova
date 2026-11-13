from dataclasses import dataclass
from decimal import Decimal

from app.domain import Opportunity, OpportunityKind


@dataclass(frozen=True, slots=True)
class OpportunityFilter:
    """Фільтри Market Scanner. None — не перевіряється."""

    kind: OpportunityKind | None = None
    buy_exchange: str | None = None
    sell_exchange: str | None = None
    min_spread: Decimal | None = None
    min_net_spread: Decimal | None = None
    min_ai_score: int | None = None
    min_rr: Decimal | None = None
    min_volume: Decimal | None = None
    hide_illiquid: bool = False
    single_fiat: bool = False


def _rank(opportunity: Opportunity) -> Decimal:
    """Чистий прибуток × доступний обсяг × ризик продавця (обсяг у фіаті: BTC і USDT порівнянні)."""
    volume = opportunity.max_volume * opportunity.buy_price
    safety = Decimal(1) - Decimal(str(opportunity.risk_score or 0))
    return opportunity.estimated_profit * volume * safety


def rank_opportunities(opportunities: list[Opportunity]) -> list[Opportunity]:
    return sorted(opportunities, key=_rank, reverse=True)


def _matches(item: Opportunity, criteria: OpportunityFilter) -> bool:
    checks = (
        criteria.kind is None or item.kind == criteria.kind,
        criteria.buy_exchange is None or item.buy_exchange == criteria.buy_exchange,
        criteria.sell_exchange is None or item.sell_exchange == criteria.sell_exchange,
        criteria.min_spread is None or item.spread_pct >= criteria.min_spread,
        criteria.min_net_spread is None or item.net_spread_pct >= criteria.min_net_spread,
        criteria.min_volume is None or item.max_volume >= criteria.min_volume,
        criteria.min_ai_score is None
        or (item.ai_score is not None and item.ai_score >= criteria.min_ai_score),
        criteria.min_rr is None or (item.rr is not None and item.rr >= criteria.min_rr),
    )
    if not all(checks):
        return False
    if criteria.single_fiat and any(
        step.fiat is not None and step.fiat != item.fiat for step in item.steps
    ):
        return False
    if criteria.hide_illiquid and item.investment is not None:
        return item.max_volume * item.buy_price >= item.investment
    return True


def filter_opportunities(
    opportunities: list[Opportunity], criteria: OpportunityFilter
) -> list[Opportunity]:
    return [item for item in opportunities if _matches(item, criteria)]
