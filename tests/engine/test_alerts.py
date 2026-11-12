from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.domain import AlertRule, Opportunity, Step
from app.engine.alerts import has_threshold, rule_matches

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def opportunity(**changes: Any) -> Opportunity:
    values: dict[str, Any] = {
        "kind": "cross_exchange",
        "asset": "USDT",
        "fiat": "UAH",
        "buy_exchange": "binance",
        "sell_exchange": "bybit",
        "buy_price": Decimal("41.00"),
        "sell_price": Decimal("41.80"),
        "steps": [
            Step(exchange="binance", action="BUY", asset="USDT", payment_method="monobank"),
            Step(exchange="bybit", action="SELL", asset="USDT", payment_method="privatbank"),
        ],
        "spread_pct": Decimal("1.95"),
        "net_spread_pct": Decimal("1.42"),
        "estimated_profit": Decimal("284.20"),
        "max_volume": Decimal("5000"),
        "rr": Decimal("3.1"),
        "ai_score": 87,
        "detected_at": NOW,
    }
    return Opportunity(**(values | changes))


def rule(**changes: Any) -> AlertRule:
    values: dict[str, Any] = {
        "id": 1,
        "user_id": 1,
        "asset": "USDT",
        "fiat": "UAH",
        "channels": ["in_app"],
    }
    return AlertRule(**(values | changes))


@pytest.mark.parametrize(
    ("thresholds", "matches"),
    [
        ({"min_spread": Decimal("1.5")}, True),
        ({"min_spread": Decimal("1.95")}, True),
        ({"min_spread": Decimal("2")}, False),
        ({"min_net_spread": Decimal("1.4")}, True),
        ({"min_net_spread": Decimal("1.5")}, False),
        ({"min_ai_score": 85}, True),
        ({"min_ai_score": 90}, False),
        ({"min_rr": Decimal("3")}, True),
        ({"min_rr": Decimal("3.5")}, False),
        ({"min_net_profit": Decimal("200")}, True),
        ({"min_net_profit": Decimal("500")}, False),
        ({"min_spread": Decimal("1.5"), "min_ai_score": 90}, False),
    ],
)
def test_all_thresholds_must_be_reached(thresholds: dict[str, Any], matches: bool) -> None:
    assert rule_matches(rule(**thresholds), opportunity()) is matches


def test_missing_score_does_not_pass_score_threshold() -> None:
    assert not rule_matches(rule(min_ai_score=10), opportunity(ai_score=None))
    assert not rule_matches(rule(min_rr=Decimal("1")), opportunity(rr=None))


def test_other_pair_does_not_match() -> None:
    assert not rule_matches(rule(fiat="EUR", min_spread=Decimal("1")), opportunity())
    assert not rule_matches(rule(asset="BTC", min_spread=Decimal("1")), opportunity())


@pytest.mark.parametrize(
    ("exchange", "matches"), [("binance", True), ("bybit", True), ("okx", False)]
)
def test_exchange_is_either_side(exchange: str, matches: bool) -> None:
    assert rule_matches(rule(exchange=exchange, min_spread=Decimal("1")), opportunity()) is matches


@pytest.mark.parametrize(
    ("payment", "matches"), [("monobank", True), ("privatbank", True), ("abank", False)]
)
def test_payment_is_any_step(payment: str, matches: bool) -> None:
    assert rule_matches(rule(payment=payment, min_spread=Decimal("1")), opportunity()) is matches


def test_disabled_rule_never_matches() -> None:
    assert not rule_matches(rule(enabled=False, min_spread=Decimal("1")), opportunity())


def test_rule_needs_at_least_one_threshold() -> None:
    assert not has_threshold(rule())
    assert has_threshold(rule(min_ai_score=0))
