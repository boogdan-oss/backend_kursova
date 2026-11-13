from datetime import UTC, datetime
from decimal import Decimal

from app.domain import Opportunity, OpportunityKind
from app.engine.ranking import OpportunityFilter, filter_opportunities, rank_opportunities

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def make(
    profit: str = "300",
    volume: str = "1000",
    risk: float | None = None,
    kind: OpportunityKind = "cross_exchange",
    spread: str = "2",
    net: str = "1.5",
    rr: str | None = "3",
    score: int | None = 70,
    buy_exchange: str = "binance",
    sell_exchange: str = "bybit",
    investment: str | None = "20000",
    buy_price: str = "41",
) -> Opportunity:
    return Opportunity(
        kind=kind,
        asset="USDT",
        fiat="UAH",
        buy_exchange=buy_exchange,
        sell_exchange=sell_exchange,
        buy_price=Decimal(buy_price),
        sell_price=Decimal("41.80"),
        steps=[],
        spread_pct=Decimal(spread),
        net_spread_pct=Decimal(net),
        estimated_profit=Decimal(profit),
        max_volume=Decimal(volume),
        rr=None if rr is None else Decimal(rr),
        risk_score=risk,
        ai_score=score,
        investment=None if investment is None else Decimal(investment),
        detected_at=NOW,
    )


def test_rank_by_profit_volume_and_merchant_risk() -> None:
    small = make(profit="100")
    big = make(profit="500")
    deep = make(profit="300", volume="5000")
    risky = make(profit="500", volume="5000", risk=0.9)

    assert rank_opportunities([small, big, risky, deep]) == [deep, big, risky, small]


def test_rank_compares_volume_in_fiat_not_in_asset() -> None:
    btc = make(profit="300", volume="0.5", buy_price="4000000")
    usdt = make(profit="300", volume="100", buy_price="41")

    assert rank_opportunities([usdt, btc]) == [btc, usdt]


def test_unknown_risk_is_neutral() -> None:
    known = make(risk=0.0)
    unknown = make(risk=None)

    assert rank_opportunities([unknown, known]) == [unknown, known]


def test_empty_filter_keeps_everything() -> None:
    items = [make(), make(kind="spread")]

    assert filter_opportunities(items, OpportunityFilter()) == items


def test_filter_by_kind_and_exchanges() -> None:
    wanted = make()
    other_kind = make(kind="chain")
    other_buy = make(buy_exchange="okx")
    other_sell = make(sell_exchange="okx")

    criteria = OpportunityFilter(
        kind="cross_exchange", buy_exchange="binance", sell_exchange="bybit"
    )

    assert filter_opportunities([wanted, other_kind, other_buy, other_sell], criteria) == [wanted]


def test_filter_by_thresholds() -> None:
    good = make()

    for criteria in (
        OpportunityFilter(min_spread=Decimal(3)),
        OpportunityFilter(min_net_spread=Decimal(2)),
        OpportunityFilter(min_ai_score=71),
        OpportunityFilter(min_rr=Decimal(4)),
        OpportunityFilter(min_volume=Decimal(2000)),
    ):
        assert filter_opportunities([good], criteria) == []

    passing = OpportunityFilter(
        min_spread=Decimal(2),
        min_net_spread=Decimal("1.5"),
        min_ai_score=70,
        min_rr=Decimal(3),
        min_volume=Decimal(1000),
    )
    assert filter_opportunities([good], passing) == [good]


def test_filter_treats_unknown_values_as_not_passing() -> None:
    unknown = make(rr=None, score=None)

    assert filter_opportunities([unknown], OpportunityFilter(min_rr=Decimal(1))) == []
    assert filter_opportunities([unknown], OpportunityFilter(min_ai_score=1)) == []


def test_hide_illiquid_drops_volume_below_deal_size() -> None:
    liquid = make(volume="500")
    thin = make(volume="400")

    assert filter_opportunities([liquid, thin], OpportunityFilter(hide_illiquid=True)) == [liquid]


def test_single_fiat_hides_chains_through_other_currencies() -> None:
    from app.domain import Step

    plain = make().model_copy(
        update={
            "steps": [
                Step(exchange="binance", action="BUY", asset="USDT", fiat="UAH"),
                Step(exchange="bybit", action="SELL", asset="USDT", fiat="UAH"),
            ]
        }
    )
    via_eur = plain.model_copy(
        update={
            "kind": "chain",
            "steps": [
                Step(exchange="binance", action="BUY", asset="USDT", fiat="UAH"),
                Step(exchange="binance", action="SELL", asset="USDT", fiat="EUR"),
                Step(exchange="binance", action="BUY", asset="USDC", fiat="EUR"),
                Step(exchange="binance", action="SELL", asset="USDC", fiat="UAH"),
            ],
        }
    )
    old_history = plain.model_copy(
        update={"steps": [Step(exchange="binance", action="BUY", asset="USDT")]}
    )

    kept = filter_opportunities([plain, via_eur, old_history], OpportunityFilter(single_fiat=True))

    assert kept == [plain, old_history]
    assert filter_opportunities([via_eur], OpportunityFilter()) == [via_eur]
