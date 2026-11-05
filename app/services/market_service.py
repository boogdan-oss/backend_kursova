from decimal import Decimal

from app.collectors.registry import get_adapter
from app.domain import Offer, Side
from app.engine.filters import filter_by_amount


async def get_live_offers(
    exchange: str,
    asset: str,
    fiat: str,
    side: Side,
    payment: str | None = None,
    amount: Decimal | None = None,
) -> list[Offer]:
    """Оголошення напряму з біржі.

    Тимчасово, доки немає collector + Redis: тоді API братиме готовий знімок, а не ходитиме
    до біржі сам.
    """
    offers = await get_adapter(exchange).fetch_offers(asset.upper(), fiat.upper(), side, payment)
    if amount is not None:
        offers = filter_by_amount(offers, amount)
    return offers
