import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from itertools import product

import networkx as nx

from app.domain import Deal, FeeSchedule, MarketSnapshot, Offer, Opportunity, Side, Step
from app.engine.fees import calculate_deal, trade_fee_rate
from app.engine.filters import filter_by_amount, filter_reliable
from app.engine.pricing import effective_price, quantity_for, typical_amount
from app.engine.ranking import rank_opportunities
from app.engine.scoring import ScoreInputs, potential_loss, risk_reward, score_opportunity
from app.engine.spreads import PERCENT_PRECISION, round_money

PairKey = tuple[str, str]
FeeMap = Mapping[str, FeeSchedule]

DEFAULT_ADVERSE_MOVE_PCT = Decimal("0.5")
MAX_CHAIN_STEPS = 4
BANK = "bank"
MIN_MARKET_OFFERS = 3
AMOUNT_PRECISION = Decimal("0.00000001")
ANOMALY_SPREAD_PCT = Decimal(5)


@dataclass(frozen=True, slots=True)
class MarketRisk:
    """Поведінка пари за останній час (із market_stats і spread_history)."""

    stability_pct: Decimal | None = None
    volatility_pct: Decimal | None = None


NO_RISK_DATA = MarketRisk()


def _group_offers(offers: Iterable[Offer]) -> dict[PairKey, dict[str, dict[Side, list[Offer]]]]:
    """Порівняння завжди в межах однієї пари: різні активи й фіати не змішуються."""
    grouped: dict[PairKey, dict[str, dict[Side, list[Offer]]]] = defaultdict(
        lambda: defaultdict(lambda: {"BUY": [], "SELL": []})
    )
    for offer in offers:
        grouped[offer.asset, offer.fiat][offer.exchange][offer.side].append(offer)
    return grouped


def _steps(pair: PairKey, buy_exchange: str, sell_exchange: str, deal: Deal) -> list[Step]:
    asset, fiat = pair
    steps = [
        Step(
            exchange=buy_exchange,
            action="BUY",
            asset=asset,
            fiat=fiat,
            price=deal.buy_price,
            amount_in=deal.investment,
            amount_out=deal.quantity,
        )
    ]
    if buy_exchange != sell_exchange:
        steps.append(
            Step(
                exchange=buy_exchange,
                action="TRANSFER",
                asset=asset,
                amount_in=deal.quantity,
                amount_out=deal.sell_quantity,
            )
        )
    steps.append(
        Step(
            exchange=sell_exchange,
            action="SELL",
            asset=asset,
            fiat=fiat,
            price=deal.sell_price,
            amount_in=deal.sell_quantity,
            amount_out=deal.investment + deal.net_profit,
        )
    )
    return steps


def _evaluate(
    pair: PairKey,
    buy_exchange: str,
    sell_exchange: str,
    buy_offers: list[Offer],
    sell_offers: list[Offer],
    fees: FeeMap,
    detected_at: datetime,
    investment: Decimal | None,
    risk: MarketRisk,
) -> Opportunity | None:
    """Угода «купити тут, продати там». None — на цю суму такої угоди немає."""
    asset, fiat = pair
    is_transfer = buy_exchange != sell_exchange
    if is_transfer and asset not in fees[buy_exchange].withdrawal_fees:
        return None

    reliable_buy, reliable_sell = filter_reliable(buy_offers), filter_reliable(sell_offers)
    amount = investment
    if amount is None:
        typical = [typical_amount(reliable_buy), typical_amount(reliable_sell)]
        amount = max((value for value in typical if value), default=None)
    sell_estimate = effective_price(reliable_sell)
    if amount is None or sell_estimate is None:
        return None

    usable_buy = filter_by_amount(reliable_buy, amount)
    buy_price = effective_price(usable_buy)
    if buy_price is None or len(usable_buy) < MIN_MARKET_OFFERS:
        return None
    quantity = quantity_for(amount, buy_price)
    usable_sell = filter_by_amount(reliable_sell, quantity * sell_estimate)
    sell_price = effective_price(usable_sell)
    if sell_price is None or len(usable_sell) < MIN_MARKET_OFFERS:
        return None

    transfer_fee = fees[buy_exchange].withdrawal_fee(asset) if is_transfer else Decimal(0)
    deal = calculate_deal(
        amount, buy_price, sell_price, fees[buy_exchange], fees[sell_exchange], transfer_fee
    )
    max_volume = min(
        sum((offer.available for offer in usable_buy), Decimal(0)),
        sum((offer.available for offer in usable_sell), Decimal(0)),
    )
    adverse_move = max(DEFAULT_ADVERSE_MOVE_PCT, risk.volatility_pct or Decimal(0))
    rr = risk_reward(deal.net_profit, potential_loss(deal.fees, amount, adverse_move))
    score = score_opportunity(
        ScoreInputs(
            net_spread_pct=deal.net_spread_pct,
            offers_count=min(len(usable_buy), len(usable_sell)),
            volume_ratio=max_volume / quantity,
            rr=rr,
            stability_pct=risk.stability_pct,
            volatility_pct=risk.volatility_pct,
        )
    )
    return Opportunity(
        kind="cross_exchange" if is_transfer else "spread",
        asset=asset,
        fiat=fiat,
        buy_exchange=buy_exchange,
        sell_exchange=sell_exchange,
        buy_price=buy_price,
        sell_price=sell_price,
        steps=_steps(pair, buy_exchange, sell_exchange, deal),
        spread_pct=deal.spread_pct,
        net_spread_pct=deal.net_spread_pct,
        estimated_profit=deal.net_profit,
        max_volume=max_volume,
        rr=None if rr is None else rr.quantize(PERCENT_PRECISION),
        ai_score=score,
        investment=amount,
        detected_at=detected_at,
    )


def evaluate_opportunities(
    offers: Iterable[Offer],
    fees: FeeMap,
    detected_at: datetime,
    investment: Decimal | None = None,
    risks: Mapping[PairKey, MarketRisk] | None = None,
) -> list[Opportunity]:
    """Усі напрямки кожної пари, зокрема збиткові: вони потрібні для історії спреду."""
    result = []
    for pair, exchanges in _group_offers(offers).items():
        risk = (risks or {}).get(pair, NO_RISK_DATA)
        for buy_exchange, sell_exchange in product(exchanges, repeat=2):
            opportunity = _evaluate(
                pair,
                buy_exchange,
                sell_exchange,
                exchanges[buy_exchange]["BUY"],
                exchanges[sell_exchange]["SELL"],
                fees,
                detected_at,
                investment,
                risk,
            )
            if opportunity is not None:
                result.append(opportunity)
    return result


def is_actionable(opportunity: Opportunity) -> bool:
    """Угода, яку можна показати як можливість: прибуткова і без ознак аномалії."""
    return opportunity.estimated_profit > 0 and opportunity.spread_pct <= ANOMALY_SPREAD_PCT


def find_opportunities(
    offers: Iterable[Offer],
    fees: FeeMap,
    detected_at: datetime,
    investment: Decimal | None = None,
    risks: Mapping[PairKey, MarketRisk] | None = None,
) -> list[Opportunity]:
    """Прибуткові після всіх комісій можливості: спред на біржі й міжбіржовий арбітраж."""
    evaluated = evaluate_opportunities(offers, fees, detected_at, investment, risks)
    return rank_opportunities([item for item in evaluated if is_actionable(item)])


def _build_graph(quotes: Iterable[MarketSnapshot], fees: FeeMap) -> nx.DiGraph:
    """Вершини (біржа, актив) і (bank, фіат), вага ребра — −log(курс)."""
    graph = nx.DiGraph()
    holders: dict[str, set[str]] = defaultdict(set)
    for quote in quotes:
        coin, bank = (quote.exchange, quote.asset), (BANK, quote.fiat)
        rate = trade_fee_rate(fees[quote.exchange])
        holders[quote.asset].add(quote.exchange)
        if quote.buy_price is not None and quote.offers_count_buy >= MIN_MARKET_OFFERS:
            weight = math.log(quote.buy_price * (1 + rate))
            graph.add_edge(
                bank, coin, weight=weight, action="BUY", quote=quote, price=quote.buy_price
            )
        if quote.sell_price is not None and quote.offers_count_sell >= MIN_MARKET_OFFERS:
            weight = -math.log(quote.sell_price * (1 - rate))
            graph.add_edge(
                coin, bank, weight=weight, action="SELL", quote=quote, price=quote.sell_price
            )

    for asset, exchanges in holders.items():
        for source, target in product(exchanges, repeat=2):
            if source != target and asset in fees[source].withdrawal_fees:
                graph.add_edge((source, asset), (target, asset), weight=0.0, action="TRANSFER")
    return graph


@dataclass(frozen=True, slots=True)
class _Walk:
    net: Decimal
    gross: Decimal
    steps: list[Step]
    capacity: Decimal


def _walk(
    graph: nx.DiGraph, cycle: list[tuple[str, str]], fees: FeeMap, investment: Decimal
) -> _Walk | None:
    """Проходить цикл у Decimal. None — якась ланка не витримує такого обсягу."""
    net = gross = investment
    first_quantity = Decimal(0)
    capacity = Decimal("Infinity")
    steps = []
    for source, target in zip(cycle, cycle[1:], strict=False):
        edge = graph.edges[source, target]
        action = edge["action"]
        if action == "TRANSFER":
            exchange, asset = source
            before = net
            net -= fees[exchange].withdrawal_fee(asset)
            if net <= 0:
                return None
            steps.append(
                Step(
                    exchange=exchange,
                    action="TRANSFER",
                    asset=asset,
                    amount_in=_amount(before),
                    amount_out=_amount(net),
                )
            )
            continue

        quote, price = edge["quote"], edge["price"]
        rate = trade_fee_rate(fees[quote.exchange])
        before = net
        if action == "BUY":
            net, gross = net / (price * (1 + rate)), gross / price
            first_quantity = first_quantity or net
            held, fiat_amount = net, before
            available, limits = quote.buy_volume, (quote.buy_min_amount, quote.buy_max_amount)
        else:
            held = net
            net, gross = net * price * (1 - rate), gross * price
            fiat_amount = net
            available, limits = quote.sell_volume, (quote.sell_min_amount, quote.sell_max_amount)
        available = quote.volume if available is None else available
        if held > available or not _within(fiat_amount, *limits):
            return None
        capacity = min(capacity, available * first_quantity / held)
        steps.append(
            Step(
                exchange=quote.exchange,
                action=action,
                asset=quote.asset,
                fiat=quote.fiat,
                price=price,
                amount_in=_amount(before),
                amount_out=_amount(net),
            )
        )
    return _Walk(net, gross, steps, capacity)


def _amount(value: Decimal) -> Decimal:
    return value.quantize(AMOUNT_PRECISION)


def _within(amount: Decimal, low: Decimal | None, high: Decimal | None) -> bool:
    """Сума кроку вкладається в ліміти хоча б одного надійного оголошення."""
    return (low is None or amount >= low) and (high is None or amount <= high)


def find_chains(
    quotes: Iterable[MarketSnapshot],
    fees: FeeMap,
    fiat: str,
    investment: Decimal,
    detected_at: datetime,
    max_steps: int = MAX_CHAIN_STEPS,
) -> list[Opportunity]:
    """Цикли від fiat і назад до нього через кілька активів чи фіатів.

    Ціни різних фіатів тут не порівнюються: кожна ланка — окрема реальна угода своєї пари.
    Цикли з одним активом і одним фіатом не потрапляють сюди: це спред або міжбіржовий арбітраж.
    """
    graph = _build_graph(quotes, fees)
    start = (BANK, fiat)
    if start not in graph:
        return []

    result = []
    for last in graph.predecessors(start):
        for path in nx.all_simple_paths(graph, start, last, cutoff=max_steps - 1):
            cycle = [*path, start]
            if nx.path_weight(graph, cycle, "weight") >= 0:
                continue
            assets = {node[1] for node in path if node[0] != BANK}
            fiats = {node[1] for node in path if node[0] == BANK}
            if len(assets) < 2 and len(fiats) < 2:
                continue
            walked = _walk(graph, cycle, fees, investment)
            if walked is None or walked.net <= investment:
                continue
            spread = ((walked.gross / investment - 1) * 100).quantize(PERCENT_PRECISION)
            if spread > ANOMALY_SPREAD_PCT:
                continue

            buy, sell = walked.steps[0], walked.steps[-1]
            assert buy.price is not None and sell.price is not None
            legs = [
                graph.edges[source, target]["quote"]
                for source, target in zip(cycle, cycle[1:], strict=False)
                if "quote" in graph.edges[source, target]
            ]
            profit = walked.net - investment
            net_spread = ((walked.net / investment - 1) * 100).quantize(PERCENT_PRECISION)
            rr = risk_reward(
                profit,
                potential_loss(walked.gross - walked.net, investment, DEFAULT_ADVERSE_MOVE_PCT),
            )
            first_quantity = quantity_for(investment, buy.price)
            result.append(
                Opportunity(
                    kind="chain",
                    asset=buy.asset,
                    fiat=fiat,
                    buy_exchange=buy.exchange,
                    sell_exchange=sell.exchange,
                    buy_price=buy.price,
                    sell_price=sell.price,
                    steps=walked.steps,
                    spread_pct=spread,
                    net_spread_pct=net_spread,
                    estimated_profit=round_money(profit),
                    max_volume=round_money(walked.capacity),
                    rr=None if rr is None else rr.quantize(PERCENT_PRECISION),
                    ai_score=score_opportunity(
                        ScoreInputs(
                            net_spread_pct=net_spread,
                            offers_count=min(
                                min(leg.offers_count_buy, leg.offers_count_sell) for leg in legs
                            ),
                            volume_ratio=walked.capacity / first_quantity,
                            rr=rr,
                            stability_pct=None,
                            volatility_pct=None,
                        )
                    ),
                    investment=investment,
                    detected_at=detected_at,
                )
            )
    return sorted(result, key=lambda item: item.net_spread_pct, reverse=True)
