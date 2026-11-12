from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Response, status
from pydantic import AfterValidator, AwareDatetime, BaseModel, Field

from app.api.deps import CODE_PATTERN, CurrentUser, SessionDep, ensure_exchange, pair_not_found
from app.db.repositories.alerts import WatchlistRow
from app.services.alert_service import (
    LimitReached,
    add_to_watchlist,
    get_watchlist,
    remove_from_watchlist,
)
from app.services.market_service import PairNotFound

router = APIRouter(tags=["watchlist"])

Code = Annotated[str, Field(pattern=CODE_PATTERN), AfterValidator(str.upper)]


class WatchlistIn(BaseModel):
    asset: Code
    fiat: Code
    exchange: str | None = Field(default=None, description="Без біржі — пара на всіх біржах")


class WatchlistItemOut(BaseModel):
    id: int
    asset: str
    fiat: str
    exchange: str | None
    created_at: AwareDatetime

    @classmethod
    def of(cls, row: WatchlistRow) -> "WatchlistItemOut":
        return cls(
            id=row.id,
            asset=row.asset,
            fiat=row.fiat,
            exchange=row.exchange,
            created_at=row.created_at,
        )


class WatchlistResponse(BaseModel):
    count: int
    items: list[WatchlistItemOut]


@router.get(
    "/watchlist",
    response_model=WatchlistResponse,
    summary="Обрані пари",
    description="Пари з обраного collector оновлює найчастіше (FR-17). Ціни — /market/snapshot.",
)
async def list_items(user: CurrentUser, session: SessionDep) -> WatchlistResponse:
    rows = await get_watchlist(session, user)
    return WatchlistResponse(count=len(rows), items=[WatchlistItemOut.of(row) for row in rows])


@router.post(
    "/watchlist",
    response_model=WatchlistItemOut,
    status_code=status.HTTP_201_CREATED,
    summary="Додати пару в обране",
    description="Якщо пара вже в обраному, повертається наявний запис з кодом 200.",
)
async def add_item(
    body: WatchlistIn, user: CurrentUser, session: SessionDep, response: Response
) -> WatchlistItemOut:
    if body.exchange is not None:
        ensure_exchange(body.exchange)
    try:
        row, created = await add_to_watchlist(session, user, body.asset, body.fiat, body.exchange)
    except PairNotFound as exc:
        raise pair_not_found(exc) from exc
    except LimitReached as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if not created:
        response.status_code = status.HTTP_200_OK
    return WatchlistItemOut.of(row)


@router.delete(
    "/watchlist/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Прибрати пару з обраного",
)
async def delete_item(
    item_id: Annotated[int, Path(ge=1)], user: CurrentUser, session: SessionDep
) -> None:
    if not await remove_from_watchlist(session, user, item_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Запис не знайдено")
