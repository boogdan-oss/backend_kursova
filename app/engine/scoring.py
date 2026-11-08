from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

WEIGHTS = tuple(Decimal(weight) for weight in ("0.30", "0.20", "0.20", "0.15", "0.10", "0.05"))

NET_SPREAD_FULL_PCT = Decimal(3)
OFFERS_FULL = Decimal(10)
RR_MIN, RR_FULL = Decimal(1), Decimal(5)
VOLUME_MIN_RATIO, VOLUME_FULL_RATIO = Decimal(1), Decimal(5)
VOLATILITY_WORST_PCT = Decimal(2)
UNKNOWN_SCORE = Decimal(50)

SCORE_SCALE = (
    (92, "Excellent Opportunity"),
    (80, "Good Opportunity"),
    (60, "Medium Opportunity"),
    (40, "High Risk"),
    (0, "Not Recommended"),
)


@dataclass(frozen=True, slots=True)
class ScoreInputs:
    net_spread_pct: Decimal
    offers_count: int
    volume_ratio: Decimal
    rr: Decimal | None
    stability_pct: Decimal | None
    volatility_pct: Decimal | None


def risk_reward(profit: Decimal, loss: Decimal) -> Decimal | None:
    """(1.6) RR = Potential Profit / Potential Loss. None — втрата невідома."""
    return profit / loss if loss > 0 else None


def potential_loss(fees: Decimal, investment: Decimal, adverse_move_pct: Decimal) -> Decimal:
    """Можлива втрата: комісії й рух ціни проти нас, поки угода незавершена."""
    return fees + investment * adverse_move_pct / 100


def normalize(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    """Лінійно переводить значення у шкалу 0–100, усе поза межами обрізає."""
    return min(max((value - low) / (high - low) * 100, Decimal(0)), Decimal(100))


def ai_score(
    s_net: Decimal,
    s_liq: Decimal,
    s_rr: Decimal,
    s_stab: Decimal,
    s_vol: Decimal,
    s_volat: Decimal,
) -> int:
    """(1.9) Зважена сума шести нормованих показників, порядок — як у WEIGHTS."""
    components = (s_net, s_liq, s_rr, s_stab, s_vol, s_volat)
    total = sum(
        (weight * value for weight, value in zip(WEIGHTS, components, strict=True)), Decimal(0)
    )
    return int(total.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def score_opportunity(inputs: ScoreInputs) -> int:
    s_rr = Decimal(0) if inputs.rr is None else normalize(inputs.rr, RR_MIN, RR_FULL)
    s_stab = UNKNOWN_SCORE if inputs.stability_pct is None else inputs.stability_pct
    s_volat = UNKNOWN_SCORE
    if inputs.volatility_pct is not None:
        s_volat = 100 - normalize(inputs.volatility_pct, Decimal(0), VOLATILITY_WORST_PCT)
    return ai_score(
        normalize(inputs.net_spread_pct, Decimal(0), NET_SPREAD_FULL_PCT),
        normalize(Decimal(inputs.offers_count), Decimal(0), OFFERS_FULL),
        s_rr,
        s_stab,
        normalize(inputs.volume_ratio, VOLUME_MIN_RATIO, VOLUME_FULL_RATIO),
        s_volat,
    )


def score_category(score: int) -> str:
    """Шкала AI Score (CLAUDE.md, розділ 6)."""
    return next(name for threshold, name in SCORE_SCALE if score >= threshold)
