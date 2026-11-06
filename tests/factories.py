from datetime import UTC, datetime
from decimal import Decimal

from app.domain import Merchant, Offer, Side

FETCHED_AT = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def make_offer(
    price: str = "41.20",
    side: Side = "BUY",
    *,
    merchant_id: str = "m1",
    orders: int = 100,
    completion_rate: str = "0.99",
    min_amount: str = "500",
    max_amount: str = "20000",
    available: str = "100",
    payments: tuple[str, ...] = ("monobank",),
) -> Offer:
    return Offer(
        exchange="binance",
        side=side,
        asset="USDT",
        fiat="UAH",
        price=Decimal(price),
        min_amount=Decimal(min_amount),
        max_amount=Decimal(max_amount),
        available=Decimal(available),
        payment_methods=list(payments),
        merchant=Merchant(
            id=merchant_id,
            nickname=merchant_id,
            orders_30d=orders,
            completion_rate=Decimal(completion_rate),
        ),
        fetched_at=FETCHED_AT,
    )
