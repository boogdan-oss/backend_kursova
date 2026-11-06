from decimal import Decimal

import pytest

from app.engine.scoring import (
    ScoreInputs,
    ai_score,
    normalize,
    potential_loss,
    risk_reward,
    score_category,
    score_opportunity,
)


def test_rr_control_example() -> None:
    """(1.6) Profit 300 / Loss 100 → RR = 3:1."""
    assert risk_reward(Decimal(300), Decimal(100)) == 3


def test_rr_without_loss_is_unknown() -> None:
    assert risk_reward(Decimal(300), Decimal(0)) is None


def test_potential_loss_is_fees_plus_adverse_move() -> None:
    assert potential_loss(Decimal(90), Decimal(20000), Decimal("0.5")) == 190


@pytest.mark.parametrize(
    ("value", "expected"),
    [("-1", 0), ("0", 0), ("1.5", 50), ("3", 100), ("9", 100)],
)
def test_normalize_is_clamped_to_scale(value: str, expected: int) -> None:
    assert normalize(Decimal(value), Decimal(0), Decimal(3)) == expected


def test_score_weights_sum_to_one_hundred() -> None:
    assert ai_score(*[Decimal(100)] * 6) == 100
    assert ai_score(*[Decimal(0)] * 6) == 0


def test_score_uses_formula_weights() -> None:
    """(1.9) 0.30·80 + 0.20·70 + 0.20·60 + 0.15·50 + 0.10·40 + 0.05·30 = 63."""
    values = [Decimal(value) for value in (80, 70, 60, 50, 40, 30)]

    assert ai_score(*values) == 63


@pytest.mark.parametrize("component", range(6))
def test_each_component_has_its_own_weight(component: int) -> None:
    weights = [30, 20, 20, 15, 10, 5]
    values = [Decimal(0)] * 6
    values[component] = Decimal(100)

    assert ai_score(*values) == weights[component]


@pytest.mark.parametrize(
    ("score", "category"),
    [
        (100, "Excellent Opportunity"),
        (92, "Excellent Opportunity"),
        (91, "Good Opportunity"),
        (80, "Good Opportunity"),
        (79, "Medium Opportunity"),
        (60, "Medium Opportunity"),
        (59, "High Risk"),
        (40, "High Risk"),
        (39, "Not Recommended"),
        (0, "Not Recommended"),
    ],
)
def test_score_scale(score: int, category: str) -> None:
    assert score_category(score) == category


def make_inputs(**overrides: Decimal | int | None) -> ScoreInputs:
    values: dict = {
        "net_spread_pct": Decimal("1.5"),
        "offers_count": 5,
        "volume_ratio": Decimal(3),
        "rr": Decimal(3),
        "stability_pct": Decimal(50),
        "volatility_pct": Decimal(1),
    }
    return ScoreInputs(**{**values, **overrides})


def test_middle_of_every_scale_gives_fifty() -> None:
    assert score_opportunity(make_inputs()) == 50


def test_perfect_opportunity_scores_one_hundred() -> None:
    inputs = make_inputs(
        net_spread_pct=Decimal(5),
        offers_count=20,
        volume_ratio=Decimal(10),
        rr=Decimal(8),
        stability_pct=Decimal(100),
        volatility_pct=Decimal(0),
    )

    assert score_opportunity(inputs) == 100


def test_unknown_history_is_neutral() -> None:
    known = make_inputs(stability_pct=Decimal(50), volatility_pct=Decimal(1))
    unknown = make_inputs(stability_pct=None, volatility_pct=None)

    assert score_opportunity(unknown) == score_opportunity(known)


def test_higher_volatility_lowers_score() -> None:
    calm = make_inputs(volatility_pct=Decimal(0))
    wild = make_inputs(volatility_pct=Decimal(2))

    assert score_opportunity(calm) > score_opportunity(wild)


def test_unknown_rr_scores_zero() -> None:
    assert score_opportunity(make_inputs(rr=None)) < score_opportunity(make_inputs())
