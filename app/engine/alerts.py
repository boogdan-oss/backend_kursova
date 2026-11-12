"""Чи підходить можливість під правило сповіщення. Чисті функції."""

from decimal import Decimal

from app.domain import AlertRule, Opportunity


def _at_least(value: Decimal | int | None, threshold: Decimal | int | None) -> bool:
    return threshold is None or (value is not None and value >= threshold)


def rule_matches(rule: AlertRule, item: Opportunity) -> bool:
    """Та сама пара, потрібна біржа й банк, і всі задані пороги досягнуті."""
    if not rule.enabled or (item.asset, item.fiat) != (rule.asset, rule.fiat):
        return False
    if rule.exchange is not None and rule.exchange not in (item.buy_exchange, item.sell_exchange):
        return False
    payments = {step.payment_method for step in item.steps}
    if rule.payment is not None and rule.payment not in payments:
        return False
    return (
        _at_least(item.spread_pct, rule.min_spread)
        and _at_least(item.net_spread_pct, rule.min_net_spread)
        and _at_least(item.ai_score, rule.min_ai_score)
        and _at_least(item.rr, rule.min_rr)
        and _at_least(item.estimated_profit, rule.min_net_profit)
    )


def has_threshold(rule: AlertRule) -> bool:
    """Правило без жодного порогу спрацьовувало б на кожну можливість пари."""
    thresholds = (
        rule.min_spread,
        rule.min_net_spread,
        rule.min_ai_score,
        rule.min_rr,
        rule.min_net_profit,
    )
    return any(value is not None for value in thresholds)
