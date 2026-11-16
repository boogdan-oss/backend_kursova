from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.domain import FeeSchedule, Offer, Opportunity, Side
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
    built = make_offer(price, side, **{**limits, **kwargs})
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


def test_cross_exchange_steps_show_what_you_give_and_get() -> None:
    offers = [
        offer("binance", "BUY", "41.00"),
        offer("binance", "SELL", "40.50"),
        offer("bybit", "BUY", "42.50"),
        offer("bybit", "SELL", "41.80"),
    ]

    (found,) = find(offers, FEES, NOW, investment=Decimal(20000))

    buy, transfer, sell = found.steps
    assert (buy.fiat, buy.amount_in) == ("UAH", Decimal(20000))
    assert buy.amount_out == transfer.amount_in
    assert transfer.fiat is None and transfer.amount_out == sell.amount_in
    assert transfer.amount_in - transfer.amount_out == FEES["binance"].withdrawal_fee("USDT")
    assert sell.fiat == "UAH"
    assert sell.amount_out - Decimal(20000) == pytest.approx(found.estimated_profit, abs=1)


def market(
    exchange: str,
    asset: str,
    fiat: str,
    buy: str | None,
    sell: str | None,
    volume: str = "100000",
    offers: int = MIN_MARKET_OFFERS,
    buy_volume: str | None = None,
    sell_volume: str | None = None,
    **limits: str,
) -> list[Offer]:
    """Ринок пари на біржі: offers однакових оголошень на кожну сторону з ціною."""
    result = []
    for side, price, total in (("BUY", buy, buy_volume), ("SELL", sell, sell_volume)):
        if price is None:
            continue
        prefix = side.lower() + "_"
        side_limits = {
            key.removeprefix(prefix): value
            for key, value in limits.items()
            if key.startswith(prefix)
        }
        available = str(Decimal(total or volume) / offers)
        result.extend(
            offer(exchange, side, price, fiat=fiat, asset=asset, available=available, **side_limits)
            for _ in range(offers)
        )
    return result


def chains(*markets: list[Offer], **kwargs: Any) -> list[Opportunity]:
    fees = kwargs.pop("fees", FEES)
    offers = [item for group in markets for item in group]
    return find_chains(offers, fees, "UAH", Decimal(20000), NOW, **kwargs)


PROFITABLE_LOOP = (
    market("binance", "USDT", "UAH", "41.00", "40.50"),
    market("binance", "USDT", "EUR", "0.97", "0.95"),
    market("binance", "USDC", "EUR", "0.93", "0.90"),
    market("binance", "USDC", "UAH", "42.00", "41.50"),
)


def test_chain_closes_on_starting_fiat_with_profit() -> None:
    found = chains(*PROFITABLE_LOOP)

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
    assert chains(*PROFITABLE_LOOP, max_steps=3) == []


def test_unprofitable_loop_gives_no_chain() -> None:
    assert (
        chains(
            market("binance", "USDT", "UAH", "41.00", "40.50"),
            market("binance", "USDT", "EUR", "0.97", "0.90"),
            market("binance", "USDC", "EUR", "0.97", "0.90"),
            market("binance", "USDC", "UAH", "42.00", "40.00"),
        )
        == []
    )


def test_plain_cross_exchange_is_not_reported_as_chain() -> None:
    assert (
        chains(
            market("binance", "USDT", "UAH", "41.00", "40.50"),
            market("bybit", "USDT", "UAH", "42.50", "41.80"),
        )
        == []
    )


def test_chain_with_transfer_pays_withdrawal_fee() -> None:
    fees = {name: item.model_copy() for name, item in FEES.items()}
    fees["binance"] = FEES["binance"].model_copy(update={"withdrawal_fees": {"USDT": Decimal(5)}})

    (found,) = chains(
        market("binance", "USDT", "UAH", "41.00", "40.50"),
        market("bybit", "USDT", "EUR", "0.97", "0.95"),
        market("bybit", "USDC", "EUR", "0.93", "0.90"),
        market("bybit", "USDC", "UAH", "42.00", "41.50"),
        fees=fees,
        max_steps=5,
    )

    assert [step.action for step in found.steps] == ["BUY", "TRANSFER", "SELL", "BUY", "SELL"]
    assert found.estimated_profit < 679


def test_chain_skips_market_without_enough_volume() -> None:
    thin = market("binance", "USDC", "UAH", "42.00", "41.50", "10")

    assert chains(*PROFITABLE_LOOP[:3], thin) == []


def test_chain_ignores_thin_market() -> None:
    thin = market("binance", "USDC", "UAH", "42.00", "41.50", offers=MIN_MARKET_OFFERS - 1)

    assert chains(*PROFITABLE_LOOP[:3], thin) == []


def test_anomalous_chain_is_not_recommended() -> None:
    """Живий випадок: цикл через тонкий ринок із нереальними цінами давав +32 %."""
    anomalous = market("binance", "USDC", "UAH", "42.00", "55.00")

    assert chains(*PROFITABLE_LOOP[:3], anomalous) == []


def test_chain_hiding_anomalous_spread_inside_is_not_recommended() -> None:
    """Живий випадок: UAH → USDT → BDT → USDT → UAH давав +4.98 %.

    Уся вигода — розрив 5.4 % між продажем USDT за BDT на одній біржі й купівлею на іншій,
    а збиткові UAH-кроки опускали цикл під загальний поріг аномалії.
    """
    loop = (
        market("bybit", "USDT", "UAH", "41.00", None),
        market("bybit", "USDT", "BDT", None, "137"),
        market("okx", "USDT", "BDT", "130", None),
        market("okx", "USDT", "UAH", None, "39.50"),
    )

    assert chains(*loop) == []
    normal = (*loop[:1], market("bybit", "USDT", "BDT", None, "133"), loop[2])
    assert chains(*normal, market("okx", "USDT", "UAH", None, "40.50"))


def test_chain_with_step_far_from_its_market_is_not_recommended() -> None:
    """Живий випадок: продаж USDT по 1.20 USD і USDC по 41 UAH разом давали +3.9 %."""
    loop = (
        market("binance", "USDT", "UAH", "41.00", "40.50"),
        market("binance", "USDT", "EUR", None, "1.10"),
        market("okx", "USDT", "EUR", "0.97", "0.95"),
        market("bybit", "USDT", "EUR", "0.97", "0.95"),
        market("binance", "USDC", "EUR", "0.93", "0.90"),
        market("binance", "USDC", "UAH", "42.00", "36.00"),
        market("okx", "USDC", "UAH", "42.00", "41.50"),
        market("bybit", "USDC", "UAH", "42.00", "41.50"),
    )

    assert chains(*loop) == []


def test_chain_prices_step_by_offers_that_accept_its_amount() -> None:
    """Дрібні лоти з кращою ціною не приймають суму кроку й не впливають на ціну."""
    tiny = market("binance", "USDT", "UAH", "38.00", None, buy_max_amount="500")

    (found,) = chains(*PROFITABLE_LOOP, tiny)

    assert found.steps[0].price == Decimal("41.00")
    assert round(found.estimated_profit) == 679


def test_chain_step_without_offers_for_its_amount_gives_nothing() -> None:
    small_only = market("binance", "USDT", "UAH", "41.00", "40.50", buy_max_amount="500")

    assert chains(small_only, *PROFITABLE_LOOP[1:]) == []


def test_chain_max_volume_is_measured_in_first_asset() -> None:
    (found,) = chains(
        market("binance", "USDT", "UAH", "41.00", "40.50", "5000"),
        market("binance", "USDT", "EUR", "0.97", "0.95", "5000"),
        market("binance", "USDC", "EUR", "0.93", "0.90", "1000"),
        market("binance", "USDC", "UAH", "42.00", "41.50", "5000"),
    )

    assert round(found.max_volume) == 979


def test_chain_ignores_side_without_price() -> None:
    missing = market("binance", "USDC", "UAH", None, None)

    assert chains(*PROFITABLE_LOOP[:3], missing) == []


def test_chain_steps_name_every_currency_and_amount() -> None:
    (best, *_) = chains(*PROFITABLE_LOOP)

    assert [step.fiat for step in best.steps] == ["UAH", "EUR", "EUR", "UAH"]
    assert best.steps[0].amount_in == Decimal(20000)
    for current, following in zip(best.steps, best.steps[1:], strict=False):
        assert current.amount_out == following.amount_in
    assert best.steps[-1].amount_out - Decimal(20000) == pytest.approx(
        best.estimated_profit, abs=Decimal("0.01")
    )


def test_chain_step_must_fit_offer_limits() -> None:
    eur_offers = market("binance", "USDC", "EUR", "0.93", "0.90", buy_min_amount="1000")

    assert chains(*PROFITABLE_LOOP[:2], eur_offers, PROFITABLE_LOOP[3]) == []
    fits = market(
        "binance", "USDC", "EUR", "0.93", "0.90", buy_min_amount="10", buy_max_amount="900"
    )
    assert chains(*PROFITABLE_LOOP[:2], fits, PROFITABLE_LOOP[3])


def test_chain_volume_uses_the_side_it_trades() -> None:
    (found,) = chains(
        market("binance", "USDT", "UAH", "41.00", "40.50", "5000"),
        market("binance", "USDT", "EUR", "0.97", "0.95", "5000"),
        market("binance", "USDC", "EUR", "0.93", "0.90", buy_volume="1000", sell_volume="1"),
        market("binance", "USDC", "UAH", "42.00", "41.50", "5000"),
    )

    assert round(found.max_volume) == 979
