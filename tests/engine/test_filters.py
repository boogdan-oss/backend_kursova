from decimal import Decimal

from app.engine.filters import (
    filter_by_amount,
    filter_by_payment,
    filter_liquid,
    filter_reliable,
)
from tests.factories import make_offer


def test_filter_by_amount_keeps_offers_within_limits() -> None:
    inside = make_offer(min_amount="500", max_amount="2000")
    too_small = make_offer(min_amount="3000", max_amount="9000")
    edge = make_offer(min_amount="100", max_amount="1500")

    assert filter_by_amount([inside, too_small, edge], Decimal(1500)) == [inside, edge]


def test_filter_by_amount_empty() -> None:
    assert filter_by_amount([], Decimal(1500)) == []


def test_filter_by_payment() -> None:
    mono = make_offer(payments=("monobank", "abank"))
    privat = make_offer(payments=("privatbank",))

    assert filter_by_payment([mono, privat], "monobank") == [mono]


def test_filter_reliable_drops_low_completion_rate() -> None:
    good = make_offer(completion_rate="0.90")
    bad = make_offer(completion_rate="0.8999")

    assert filter_reliable([good, bad]) == [good]


def test_filter_reliable_drops_merchants_without_history() -> None:
    good = make_offer(orders=50)
    newcomer = make_offer(orders=49)

    assert filter_reliable([good, newcomer]) == [good]


def test_filter_liquid_drops_small_offers() -> None:
    deep = make_offer(available="100")
    edge = make_offer(available="50")
    thin = make_offer(available="49.99")

    assert filter_liquid([deep, edge, thin], Decimal(50)) == [deep, edge]
