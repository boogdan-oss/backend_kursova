from typing import Annotated

from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AfterValidator
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.registry import available_exchanges
from app.core.security import InvalidToken, decode_token
from app.db.models import User, UserRole
from app.db.repositories.users import get_user
from app.db.session import get_session
from app.services.auth_service import AccountBlocked, ensure_active
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


bearer = HTTPBearer(
    auto_error=False,
    description="Access-токен з /auth/login або /auth/register",
)


def unauthorized(detail: str = "Потрібно увійти") -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"}
    )


async def current_user(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> User:
    """Користувач з access-токена. Заблокований — 403, без токена чи з недійсним — 401."""
    if credentials is None:
        raise unauthorized()
    try:
        claims = decode_token(credentials.credentials, "access")
    except InvalidToken:
        raise unauthorized("Сесія закінчилась або токен недійсний") from None
    user = await get_user(session, claims.user_id)
    if user is None:
        raise unauthorized("Користувача не знайдено")
    try:
        await ensure_active(session, user)
    except AccountBlocked as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from None
    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def require_admin(user: CurrentUser) -> User:
    if user.role != UserRole.ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Потрібні права адміністратора")
    return user


AdminUser = Annotated[User, Depends(require_admin)]


async def optional_user(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> User | None:
    """Користувач, якщо прийшов токен; без токена — None. Недійсний токен — 401."""
    if credentials is None:
        return None
    return await current_user(session, credentials)


OptionalUser = Annotated[User | None, Depends(optional_user)]
