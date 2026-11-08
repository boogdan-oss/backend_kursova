from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.domain import FeeSchedule, MarketSnapshot, Offer, Opportunity, Side
from app.engine.arbitrage import (
    ANOMALY_SPREAD_PCT,
    MIN_MARKET_OFFERS,
    MarketRisk,
    evaluate_opportunities,
    find_chains,
    find_opportunities,
)
from tests.factories import make_offer

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
TRANSFER = {"USDT": Decimal(1), "BTC": Decimal("0.0005")}
FEES = {
    "binance": FeeSchedule(exchange="binance", withdrawal_fees=TRANSFER),
    "bybit": FeeSchedule(exchange="bybit", withdrawal_fees=TRANSFER),
    "okx": FeeSchedule(exchange="okx", withdrawal_fees=TRANSFER),
}


def offer(
    exchange: str,
    side: Side,
    price: str,
    fiat: str = "UAH",
    asset: str = "USDT",
    **kwargs: str | int,
) -> Offer:
    limits = {"min_amount": "100", "max_amount": "1000000", "available": "5000"}
    built = make_offer(price, side, **{**limits, **kwargs})  # type: ignore[arg-type]
    return built.model_copy(update={"exchange": exchange, "asset": asset, "fiat": fiat})


def repeated(offers: list[Offer]) -> list[Offer]:
    """Кожне оголошення кілька разів: одиничні лоти рушій ринком не вважає."""
    return [item for item in offers for _ in range(MIN_MARKET_OFFERS)]


def find(offers: list[Offer], *args: Any, **kwargs: Any) -> list[Opportunity]:
    return find_opportunities(repeated(offers), *args, **kwargs)


def evaluate(offers: list[Offer], *args: Any, **kwargs: Any) -> list[Opportunity]:
    return evaluate_opportunities(repeated(offers), *args, **kwargs)


def test_empty_market() -> None:
    assert find([], FEES, NOW) == []


def test_exchange_without_one_side_gives_nothing() -> None:
    assert find([offer("binance", "BUY", "41.00")], FEES, NOW) == []


def test_single_exchange_has_no_cross_exchange_opportunity() -> None:
    offers = [offer("binance", "BUY", "41.00"), offer("binance", "SELL", "41.50")]

    found = find(offers, FEES, NOW, investment=Decimal(20000))

    assert [item.kind for item in found] == ["spread"]
    assert found[0].buy_exchange == found[0].sell_exchange == "binance"
    assert [step.action for step in found[0].steps] == ["BUY", "SELL"]


def test_normal_market_with_positive_bid_ask_gap_has_no_spread_opportunity() -> None:
    offers = [offer("binance", "BUY", "41.85"), offer("binance", "SELL", "41.20")]

    assert find(offers, FEES, NOW) == []


def test_cross_exchange_control_example() -> None:
    offers = [
        offer("binance", "BUY", "41.00"),
        offer("binance", "SELL", "40.50"),
        offer("bybit", "BUY", "42.50"),
        offer("bybit", "SELL", "41.80"),
    ]

    (found,) = find(offers, FEES, NOW, investment=Decimal(20000))

    assert found.kind == "cross_exchange"
    assert (found.buy_exchange, found.sell_exchange) == ("binance", "bybit")
    assert (found.buy_price, found.sell_price) == (Decimal("41.00"), Decimal("41.80"))
    assert found.spread_pct == Decimal("1.9512")
    assert round(found.estimated_profit) == 348
    assert found.net_spread_pct == pytest.approx(Decimal("1.74"), abs=Decimal("0.01"))
    assert found.investment == 20000
    assert found.detected_at == NOW
    assert [step.action for step in found.steps] == ["BUY", "TRANSFER", "SELL"]
    assert [step.exchange for step in found.steps] == ["binance", "binance", "bybit"]


def test_transfer_fee_can_eat_profit() -> None:
    offers = [
        offer("binance", "BUY", "41.00"),
        offer("binance", "SELL", "40.50"),
        offer("bybit", "BUY", "42.50"),
        offer("bybit", "SELL", "41.05"),
    ]

    assert find(offers, FEES, NOW, investment=Decimal(2000)) == []


def test_percent_fee_can_eat_profit() -> None:
    fees = {
        name: item.model_copy(update={"p2p_fee_pct": Decimal(1)}) for name, item in FEES.items()
    }
    offers = [
        offer("binance", "BUY", "41.00"),
        offer("binance", "SELL", "40.50"),
        offer("bybit", "BUY", "42.50"),
        offer("bybit", "SELL", "41.50"),
    ]

    assert find(offers, fees, NOW, investment=Decimal(20000)) == []


def test_different_fiats_are_not_mixed() -> None:
    offers = [
        offer("binance", "BUY", "0.95", fiat="EUR"),
        offer("binance", "SELL", "0.90", fiat="EUR"),
        offer("bybit", "BUY", "42.50"),
        offer("bybit", "SELL", "41.80"),
    ]

    assert find(offers, FEES, NOW) == []


def test_different_assets_are_not_mixed() -> None:
    offers = [
        offer("binance", "BUY", "41.00", asset="USDC"),
        offer("binance", "SELL", "40.50", asset="USDC"),
        offer("bybit", "BUY", "42.50"),
        offer("bybit", "SELL", "41.80"),
    ]

    assert find(offers, FEES, NOW) == []


def test_asset_with_unknown_withdrawal_fee_is_skipped() -> None:
    """Без комісії виводу чистий прибуток був би завищений, тому такий арбітраж не рахуємо."""
    offers = [
        offer("binance", "BUY", "41.00", asset="DOGE"),
        offer("binance", "SELL", "40.50", asset="DOGE"),
        offer("bybit", "BUY", "42.50", asset="DOGE"),
        offer("bybit", "SELL", "41.80", asset="DOGE"),
    ]

    assert find(offers, FEES, NOW) == []


def test_unreliable_merchants_do_not_create_opportunity() -> None:
    offers = [
        offer("binance", "BUY", "41.00", orders=3),
        offer("binance", "BUY", "42.00"),
        offer("binance", "SELL", "40.50"),
        offer("bybit", "BUY", "42.50"),
        offer("bybit", "SELL", "41.80"),
    ]

    assert find(offers, FEES, NOW, investment=Decimal(20000)) == []


def test_offers_outside_deal_limits_are_ignored() -> None:
    big_lot = offer("bybit", "SELL", "49.00", min_amount="350000", max_amount="900000")
    offers = [
        offer("binance", "BUY", "41.00"),
        offer("binance", "SELL", "40.50"),
        offer("bybit", "BUY", "42.50"),
        big_lot,
        offer("bybit", "SELL", "41.80"),
    ]

    (found,) = find(offers, FEES, NOW, investment=Decimal(20000))

    assert found.sell_price == Decimal("41.80")


def test_max_volume_is_limited_by_thinner_side() -> None:
    offers = [
        offer("binance", "BUY", "41.00", available="700"),
        offer("binance", "BUY", "41.00", available="300"),
        offer("binance", "SELL", "40.50"),
        offer("bybit", "BUY", "42.50"),
        offer("bybit", "SELL", "41.80", available="400"),
    ]

    (found,) = find(offers, FEES, NOW, investment=Decimal(10000))

    assert found.max_volume == 400 * MIN_MARKET_OFFERS


def test_default_investment_is_typical_deal_size() -> None:
    offers = [
        offer("binance", "BUY", "41.00", min_amount="2000"),
        offer("binance", "SELL", "40.50", min_amount="2000"),
        offer("bybit", "BUY", "42.50", min_amount="5000"),
        offer("bybit", "SELL", "41.80", min_amount="5000"),
    ]

    (found,) = find(offers, FEES, NOW)

    assert found.investment == 5000


def test_rr_and_ai_score_are_filled() -> None:
    offers = [
        offer("binance", "BUY", "41.00"),
        offer("binance", "SELL", "40.50"),
        offer("bybit", "BUY", "42.50"),
        offer("bybit", "SELL", "41.80"),
    ]

    (found,) = find(offers, FEES, NOW, investment=Decimal(20000))

    assert found.rr is not None and found.rr > 0
    assert found.ai_score is not None and 0 <= found.ai_score <= 100


def test_market_risk_affects_score_and_rr() -> None:
    offers = [
        offer("binance", "BUY", "41.00"),
        offer("binance", "SELL", "40.50"),
        offer("bybit", "BUY", "42.50"),
        offer("bybit", "SELL", "41.80"),
    ]
    calm = {("USDT", "UAH"): MarketRisk(Decimal(100), Decimal(0))}
    wild = {("USDT", "UAH"): MarketRisk(Decimal(0), Decimal(2))}

    (stable,) = find(offers, FEES, NOW, Decimal(20000), calm)
    (risky,) = find(offers, FEES, NOW, Decimal(20000), wild)

    assert stable.ai_score is not None and risky.ai_score is not None
    assert stable.ai_score > risky.ai_score
    assert stable.rr is not None and risky.rr is not None
    assert stable.rr > risky.rr


def test_evaluate_keeps_losing_directions_for_history() -> None:
    offers = [
        offer("binance", "BUY", "41.00"),
        offer("binance", "SELL", "40.50"),
        offer("bybit", "BUY", "42.50"),
        offer("bybit", "SELL", "41.80"),
    ]

    evaluated = evaluate(offers, FEES, NOW, investment=Decimal(20000))

    directions = {(item.buy_exchange, item.sell_exchange) for item in evaluated}
    assert ("binance", "bybit") in directions
    assert ("bybit", "binance") in directions
    assert any(item.net_spread_pct < 0 for item in evaluated)


def test_market_of_one_or_two_lots_is_not_a_market() -> None:
    offers = [
        offer("binance", "BUY", "41.00"),
        offer("binance", "BUY", "41.00"),
        offer("bybit", "SELL", "41.80"),
        offer("bybit", "SELL", "41.80"),
    ]

    assert find_opportunities(offers, FEES, NOW, investment=Decimal(20000)) == []


def test_three_lots_are_enough() -> None:
    offers = [
        *[offer("binance", "BUY", "41.00") for _ in range(MIN_MARKET_OFFERS)],
        *[offer("bybit", "SELL", "41.80") for _ in range(MIN_MARKET_OFFERS)],
    ]

    assert len(find_opportunities(offers, FEES, NOW, investment=Decimal(20000))) == 1


def test_anomalous_spread_is_not_recommended_but_stays_in_evaluation() -> None:
    """Живий випадок: BTC/USD на Bybit, купівля 80 238 і продаж 113 477 (+41 %)."""
    offers = [
        offer("bybit", "BUY", "80237.96", asset="BTC", fiat="USD"),
        offer("bybit", "SELL", "113477", asset="BTC", fiat="USD"),
    ]

    assert find(offers, FEES, NOW, investment=Decimal(5000)) == []
    (evaluated,) = evaluate(offers, FEES, NOW, investment=Decimal(5000))
    assert evaluated.spread_pct > ANOMALY_SPREAD_PCT


def test_spread_on_the_anomaly_threshold_is_still_real() -> None:
    offers = [offer("bybit", "BUY", "100"), offer("bybit", "SELL", "105")]

    (found,) = find(offers, FEES, NOW, investment=Decimal(20000))

    assert found.spread_pct == ANOMALY_SPREAD_PCT


def test_best_opportunity_goes_first() -> None:
    offers = [
        offer("binance", "BUY", "41.00"),
        offer("binance", "SELL", "40.50"),
        offer("okx", "BUY", "42.50"),
        offer("okx", "SELL", "41.60"),
        offer("bybit", "BUY", "42.50"),
        offer("bybit", "SELL", "42.00"),
    ]

    found = find(offers, FEES, NOW, investment=Decimal(20000))

    assert [item.sell_exchange for item in found] == ["bybit", "okx"]


def snapshot(
    exchange: str,
    asset: str,
    fiat: str,
    buy: str | None,
    sell: str | None,
    volume: str = "100000",
    offers: int = MIN_MARKET_OFFERS,
) -> MarketSnapshot:
    return MarketSnapshot(
        exchange=exchange,
        asset=asset,
        fiat=fiat,
        buy_price=None if buy is None else Decimal(buy),
        sell_price=None if sell is None else Decimal(sell),
        volume=Decimal(volume),
        offers_count_buy=offers,
        offers_count_sell=offers,
        fetched_at=NOW,
    )


PROFITABLE_LOOP = [
    snapshot("binance", "USDT", "UAH", "41.00", "40.50"),
    snapshot("binance", "USDT", "EUR", "0.97", "0.95"),
    snapshot("binance", "USDC", "EUR", "0.93", "0.90"),
    snapshot("binance", "USDC", "UAH", "42.00", "41.50"),
]


def test_chain_closes_on_starting_fiat_with_profit() -> None:
    found = find_chains(PROFITABLE_LOOP, FEES, "UAH", Decimal(20000), NOW)

    assert found
    best = found[0]
    assert best.kind == "chain"
    assert best.fiat == "UAH"
    assert best.net_spread_pct > 0
    assert best.estimated_profit > 0
    assert [step.action for step in best.steps] == ["BUY", "SELL", "BUY", "SELL"]
    assert [step.asset for step in best.steps] == ["USDT", "USDT", "USDC", "USDC"]
    assert round(best.estimated_profit) == 679


def test_chain_is_limited_by_max_steps() -> None:
    assert find_chains(PROFITABLE_LOOP, FEES, "UAH", Decimal(20000), NOW, max_steps=3) == []


def test_unprofitable_loop_gives_no_chain() -> None:
    loop = [
        snapshot("binance", "USDT", "UAH", "41.00", "40.50"),
        snapshot("binance", "USDT", "EUR", "0.97", "0.90"),
        snapshot("binance", "USDC", "EUR", "0.97", "0.90"),
        snapshot("binance", "USDC", "UAH", "42.00", "40.00"),
    ]

    assert find_chains(loop, FEES, "UAH", Decimal(20000), NOW) == []


def test_plain_cross_exchange_is_not_reported_as_chain() -> None:
    quotes = [
        snapshot("binance", "USDT", "UAH", "41.00", "40.50"),
        snapshot("bybit", "USDT", "UAH", "42.50", "41.80"),
    ]

    assert find_chains(quotes, FEES, "UAH", Decimal(20000), NOW) == []


def test_chain_with_transfer_pays_withdrawal_fee() -> None:
    loop = [
        snapshot("binance", "USDT", "UAH", "41.00", "40.50"),
        snapshot("bybit", "USDT", "EUR", "0.97", "0.95"),
        snapshot("bybit", "USDC", "EUR", "0.93", "0.90"),
        snapshot("bybit", "USDC", "UAH", "42.00", "41.50"),
    ]
    fees = {name: item.model_copy() for name, item in FEES.items()}
    fees["binance"] = FEES["binance"].model_copy(update={"withdrawal_fees": {"USDT": Decimal(5)}})

    (found,) = find_chains(loop, fees, "UAH", Decimal(20000), NOW, max_steps=5)

    assert [step.action for step in found.steps] == ["BUY", "TRANSFER", "SELL", "BUY", "SELL"]
    assert found.estimated_profit < 679


def test_chain_skips_market_without_enough_volume() -> None:
    thin = [*PROFITABLE_LOOP[:3], snapshot("binance", "USDC", "UAH", "42.00", "41.50", "10")]

    assert find_chains(thin, FEES, "UAH", Decimal(20000), NOW) == []


def test_chain_ignores_thin_market() -> None:
    thin = [
        *PROFITABLE_LOOP[:3],
        snapshot("binance", "USDC", "UAH", "42.00", "41.50", offers=MIN_MARKET_OFFERS - 1),
    ]

    assert find_chains(thin, FEES, "UAH", Decimal(20000), NOW) == []


def test_anomalous_chain_is_not_recommended() -> None:
    """Живий випадок: цикл через тонкий ринок із нереальними цінами давав +32 %."""
    loop = [*PROFITABLE_LOOP[:3], snapshot("binance", "USDC", "UAH", "42.00", "55.00")]

    assert find_chains(loop, FEES, "UAH", Decimal(20000), NOW) == []


def test_chain_max_volume_is_measured_in_first_asset() -> None:
    loop = [
        snapshot("binance", "USDT", "UAH", "41.00", "40.50", "5000"),
        snapshot("binance", "USDT", "EUR", "0.97", "0.95", "5000"),
        snapshot("binance", "USDC", "EUR", "0.93", "0.90", "1000"),
        snapshot("binance", "USDC", "UAH", "42.00", "41.50", "5000"),
    ]

    (found,) = find_chains(loop, FEES, "UAH", Decimal(20000), NOW)

    assert round(found.max_volume) == 979


def test_chain_ignores_side_without_price() -> None:
    missing = [*PROFITABLE_LOOP[:3], snapshot("binance", "USDC", "UAH", None, None)]

    assert find_chains(missing, FEES, "UAH", Decimal(20000), NOW) == []
