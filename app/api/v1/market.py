from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import AwareDatetime, BaseModel

from app.api.deps import (
    AssetQuery,
    ExchangeQuery,
    FiatQuery,
    SessionDep,
    ensure_exchange,
    pair_not_found,
)
from app.db.repositories.market import find_markets
from app.domain import Freshness, MarketSnapshot
from app.services.market_service import PairNotFound, get_history, get_snapshot

router = APIRouter(tags=["market"])

HistoryInterval = Literal["1m", "5m", "15m", "1h", "1d"]
INTERVALS: dict[str, timedelta] = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
}
MAX_HISTORY_POINTS = 2000
DEFAULT_HISTORY_RANGE = timedelta(hours=1)


class MarketItem(BaseModel):
    exchange: str
    asset: str
    fiat: str
    payment_methods: list[str]
    buy_fetched_at: datetime | None
    sell_fetched_at: datetime | None


class MarketsResponse(BaseModel):
    count: int
    assets: list[str]
    fiats: list[str]
    items: list[MarketItem]


class SnapshotResponse(BaseModel):
    asset: str
    fiat: str
    freshness: list[Freshness]
    queued: bool
    items: list[MarketSnapshot]


class HistoryItem(BaseModel):
    time: datetime
    buy_price: Decimal | None
    sell_price: Decimal | None
    spread_pct: Decimal | None
    volume: Decimal
    samples: int  # скільки зборів потрапило в інтервал


class HistoryResponse(BaseModel):
    exchange: str
    asset: str
    fiat: str
    interval: HistoryInterval
    count: int
    items: list[HistoryItem]


def _rounded(value: Decimal | None) -> Decimal | None:
    return None if value is None else round(value, 4)


@router.get(
    "/markets",
    response_model=MarketsResponse,
    summary="Доступні пари й способи оплати по біржах",
    description=(
        "Довідник береться з бірж автоматично. buy_fetched_at і sell_fetched_at — коли "
        "сторону пари збирали востаннє; null — ще не збирали."
    ),
)
async def list_markets(
    session: SessionDep,
    exchange: ExchangeQuery,
    asset: Annotated[str | None, Query(description="Фільтр за активом")] = None,
    fiat: Annotated[str | None, Query(description="Фільтр за фіатом")] = None,
) -> MarketsResponse:
    markets = await find_markets(
        session, exchange, asset.upper() if asset else None, fiat.upper() if fiat else None
    )
    return MarketsResponse(
        count=len(markets),
        assets=sorted({market.asset for market in markets}),
        fiats=sorted({market.fiat for market in markets}),
        items=[
            MarketItem(
                exchange=market.exchange,
                asset=market.asset,
                fiat=market.fiat,
                payment_methods=market.payment_methods,
                buy_fetched_at=market.buy_fetched_at,
                sell_fetched_at=market.sell_fetched_at,
            )
            for market in markets
        ],
    )


@router.get(
    "/market/snapshot",
    response_model=SnapshotResponse,
    summary="Ефективні ціни пари по біржах і способах оплати",
    description=(
        "Ефективна ціна — медіана топ-5 оголошень надійних продавців (захист від фейкових "
        "лотів). buy_price — за скільки можна купити, sell_price — за скільки продати.\n\n"
        "Перший елемент біржі — усі способи оплати (payment_method = null), далі — кожен окремо."
        "\n\namount відкидає оголошення, у ліміти яких сума не вкладається: без нього у ціну "
        "входять і великі лоти, недоступні для звичайної угоди."
    ),
)
async def market_snapshot(
    asset: AssetQuery,
    fiat: FiatQuery,
    session: SessionDep,
    exchange: ExchangeQuery,
    amount: Annotated[Decimal | None, Query(gt=0, description="Сума угоди у фіаті")] = None,
) -> SnapshotResponse:
    try:
        view = await get_snapshot(session, asset, fiat, exchange, amount)
    except PairNotFound as exc:
        raise pair_not_found(exc) from exc
    return SnapshotResponse(
        asset=asset, fiat=fiat, freshness=view.freshness, queued=view.queued, items=view.items
    )


@router.get(
    "/market/history",
    response_model=HistoryResponse,
    summary="Історія ефективних цін і спреду",
    description=(
        "Усереднені значення за інтервал із таблиці market_stats. "
        "Без from і to — за останню годину."
    ),
)
async def market_history(
    asset: AssetQuery,
    fiat: FiatQuery,
    session: SessionDep,
    exchange: Annotated[str, Query(description="Код біржі")] = "binance",
    start: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    end: Annotated[AwareDatetime | None, Query(alias="to")] = None,
    interval: HistoryInterval = "1m",
) -> HistoryResponse:
    ensure_exchange(exchange)
    period_end = end or datetime.now(UTC)
    period_start = start or period_end - DEFAULT_HISTORY_RANGE
    if period_start >= period_end:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "from має бути раніше за to")
    if (period_end - period_start) / INTERVALS[interval] > MAX_HISTORY_POINTS:
        detail = f"Забагато точок (макс. {MAX_HISTORY_POINTS}): збільште interval"
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail)

    try:
        points = await get_history(
            session, exchange, asset, fiat, period_start, period_end, INTERVALS[interval]
        )
    except PairNotFound as exc:
        raise pair_not_found(exc) from exc
    return HistoryResponse(
        exchange=exchange,
        asset=asset,
        fiat=fiat,
        interval=interval,
        count=len(points),
        items=[
            HistoryItem(
                time=point.time,
                buy_price=_rounded(point.buy_price),
                sell_price=_rounded(point.sell_price),
                spread_pct=_rounded(point.spread),
                volume=round(point.volume, 4),
                samples=point.samples,
            )
            for point in points
        ],
    )
