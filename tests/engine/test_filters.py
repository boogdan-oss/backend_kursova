from datetime import UTC, datetime
from decimal import Decimal

from app.domain import Merchant, Offer
from app.engine.filters import filter_by_amount


def _offer(min_amount: str, max_amount: str) -> Offer:
    return Offer(
        exchange="binance",
        side="BUY",
        asset="USDT",
        fiat="UAH",
        price=Decimal("41.20"),
        min_amount=Decimal(min_amount),
        max_amount=Decimal(max_amount),
        available=Decimal(1000),
        payment_methods=["monobank"],
        merchant=Merchant(id="m", nickname="m", orders_30d=100, completion_rate=Decimal("0.99")),
        fetched_at=datetime.now(UTC),
    )


def test_filter_by_amount_keeps_offers_within_limits() -> None:
    inside, too_small, edge = _offer("500", "2000"), _offer("3000", "9000"), _offer("100", "1500")

    assert filter_by_amount([inside, too_small, edge], Decimal(1500)) == [inside, edge]


def test_filter_by_amount_empty() -> None:
    assert filter_by_amount([], Decimal(1500)) == []
