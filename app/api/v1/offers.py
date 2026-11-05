from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from app.collectors.base import ExchangeError
from app.collectors.registry import available_exchanges
from app.domain import Offer, Side
from app.services.market_service import get_live_offers

router = APIRouter(tags=["offers"])


class OffersResponse(BaseModel):
    exchange: str
    asset: str
    fiat: str
    side: Side
    count: int
    time: datetime
    items: list[Offer]


@router.get(
    "/offers",
    response_model=OffersResponse,
    summary="P2P-оголошення біржі для пари",
    description=(
        "side — з погляду користувача: BUY — оголошення, де можна купити asset за fiat "
        "(від найдешевшого), SELL — де можна продати (від найдорожчого)."
    ),
)
async def list_offers(
    asset: Annotated[str, Query(examples=["USDT"], description="Крипто-актив")],
    fiat: Annotated[str, Query(examples=["UAH"], description="Фіатна валюта, ISO 4217")],
    side: Annotated[Side, Query(examples=["BUY"])],
    exchange: Annotated[str, Query(description="Код біржі")] = "binance",
    payment: Annotated[str | None, Query(description="Спосіб оплати, напр. monobank")] = None,
    amount: Annotated[Decimal | None, Query(gt=0, description="Сума угоди у фіаті")] = None,
) -> OffersResponse:
    if exchange not in available_exchanges():
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Біржа «{exchange}» не підтримується. Доступні: {', '.join(available_exchanges())}",
        )
    try:
        offers = await get_live_offers(exchange, asset, fiat, side, payment, amount)
    except ExchangeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Біржа недоступна: {exc.detail}") from exc
    return OffersResponse(
        exchange=exchange,
        asset=asset.upper(),
        fiat=fiat.upper(),
        side=side,
        count=len(offers),
        time=datetime.now(UTC),
        items=offers,
    )
