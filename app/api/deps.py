from typing import Annotated

from fastapi import Depends, HTTPException, Query, status
from pydantic import AfterValidator
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.registry import available_exchanges
from app.db.session import get_session
from app.services.market_service import PairNotFound

CODE_PATTERN = r"^[A-Za-z0-9]{1,16}$"

SessionDep = Annotated[AsyncSession, Depends(get_session)]
AssetQuery = Annotated[
    str,
    Query(pattern=CODE_PATTERN, examples=["USDT"], description="Крипто-актив"),
    AfterValidator(str.upper),
]
FiatQuery = Annotated[
    str,
    Query(pattern=CODE_PATTERN, examples=["UAH"], description="Фіатна валюта, ISO 4217"),
    AfterValidator(str.upper),
]


def ensure_exchange(exchange: str) -> str:
    if exchange not in available_exchanges():
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Біржа «{exchange}» не підтримується. Доступні: {', '.join(available_exchanges())}",
        )
    return exchange


def optional_exchange(
    exchange: Annotated[str | None, Query(description="Код біржі; без нього — усі")] = None,
) -> str | None:
    return None if exchange is None else ensure_exchange(exchange)


ExchangeQuery = Annotated[str | None, Depends(optional_exchange)]


def pair_not_found(exc: PairNotFound) -> HTTPException:
    return HTTPException(
        status.HTTP_404_NOT_FOUND,
        f"Пари {exc.asset}/{exc.fiat} немає в довіднику бірж. Доступні пари — GET /api/v1/markets",
    )
