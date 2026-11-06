from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.api.deps import AssetQuery, ExchangeQuery, FiatQuery, SessionDep, pair_not_found
from app.domain import Freshness, Offer, Side
from app.services.market_service import PairNotFound, get_offers

router = APIRouter(tags=["offers"])


class OffersResponse(BaseModel):
    asset: str
    fiat: str
    side: Side
    count: int
    freshness: list[Freshness]
    queued: bool
    items: list[Offer]


@router.get(
    "/offers",
    response_model=OffersResponse,
    summary="P2P-оголошення пари з останнього збору",
    description=(
        "Дані з БД: API не ходить до бірж, їх збирає collector.\n\n"
        "side — з погляду користувача: BUY — оголошення, де можна купити asset за fiat "
        "(від найдешевшого), SELL — де можна продати (від найдорожчого).\n\n"
        "freshness показує вік даних. Якщо вони застарілі, queued = true: collector підняв "
        "пріоритет пари, і за кілька секунд дані оновляться.\n\n"
        "Обмеження: collector зберігає топ-20 оголошень кожної сторони, і payment та amount "
        "фільтрують лише їх. Порожній результат не означає, що таких оголошень на біржі немає."
    ),
)
async def list_offers(
    asset: AssetQuery,
    fiat: FiatQuery,
    side: Annotated[Side, Query(examples=["BUY"])],
    session: SessionDep,
    exchange: ExchangeQuery,
    payment: Annotated[str | None, Query(description="Спосіб оплати, напр. monobank")] = None,
    amount: Annotated[Decimal | None, Query(gt=0, description="Сума угоди у фіаті")] = None,
) -> OffersResponse:
    try:
        view = await get_offers(session, asset, fiat, side, exchange, payment, amount)
    except PairNotFound as exc:
        raise pair_not_found(exc) from exc
    return OffersResponse(
        asset=asset,
        fiat=fiat,
        side=side,
        count=len(view.offers),
        freshness=view.freshness,
        queued=view.queued,
        items=view.offers,
    )
