from datetime import UTC, datetime
from decimal import Decimal

from app.db.repositories.offers import merchant_rows
from app.domain import Merchant, Offer


def make_offer(merchant_id: str, orders: int) -> Offer:
    return Offer(
        exchange="binance",
        side="BUY",
        asset="USDT",
        fiat="UAH",
        price=Decimal("41.20"),
        min_amount=Decimal(500),
        max_amount=Decimal(20000),
        available=Decimal(100),
        payment_methods=["monobank"],
        merchant=Merchant(
            id=merchant_id, nickname="trader", orders_30d=orders, completion_rate=Decimal("0.98")
        ),
        fetched_at=datetime(2026, 11, 5, tzinfo=UTC),
    )


def test_merchant_rows_keep_one_row_per_merchant() -> None:
    rows = merchant_rows(1, [make_offer("a", 10), make_offer("a", 20), make_offer("b", 5)])
    assert sorted(row["external_id"] for row in rows) == ["a", "b"]
    assert next(row for row in rows if row["external_id"] == "a")["orders_30d"] == 20
