from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from pydantic import AwareDatetime, BaseModel, EmailStr, Field
from redis.exceptions import RedisError

from app.api.deps import CODE_PATTERN, CurrentUser, SessionDep
from app.db.models import User, UserRole, UserStatus
from app.pubsub import get_redis
from app.services.auth_service import (
    AccountBlocked,
    InvalidCredentials,
    InvalidRefreshToken,
    TokenPair,
    TooManyAttempts,
    UserExists,
    change_password,
    login,
    logout,
    refresh,
    register,
)
from app.services.google_auth_service import (
    GoogleAuthError,
    allowed_origin,
    finish,
    redeem,
    start_url,
)

router = APIRouter(tags=["auth"])

Password = Annotated[str, Field(min_length=8, max_length=128)]


class UserSettings(BaseModel):
    """Персональні налаштування. Усі поля необов'язкові; null у PATCH прибирає поле."""

    default_asset: Annotated[str | None, Field(pattern=CODE_PATTERN)] = None
    default_fiat: Annotated[str | None, Field(pattern=CODE_PATTERN)] = None
    default_payment: Annotated[str | None, Field(max_length=64)] = None
    default_investment: Annotated[Decimal | None, Field(gt=0)] = None
    min_rr: Annotated[Decimal | None, Field(ge=0)] = None
    extra_fees: Annotated[Decimal | None, Field(ge=0, description="Додаткові комісії у фіаті")] = (
        None
    )
    hide_illiquid: bool | None = None


class UserOut(BaseModel):
    id: int
    username: str
    email: str
    role: UserRole
    status: UserStatus
    telegram_connected: bool
    google_connected: bool
    settings: UserSettings
    created_at: AwareDatetime

    @classmethod
    def of(cls, user: User) -> "UserOut":
        return cls(
            id=user.id,
            username=user.username,
            email=user.email,
            role=user.role,
            status=user.status,
            telegram_connected=user.telegram_chat_id is not None,
            google_connected=user.google_sub is not None,
            settings=UserSettings.model_validate(user.settings),
            created_at=user.created_at,
        )


class RegisterRequest(BaseModel):
    username: Annotated[str, Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_.]+$")]
    email: EmailStr
    password: Password


class LoginRequest(BaseModel):
    login: Annotated[str, Field(min_length=1, max_length=255, description="username або email")]
    password: Annotated[str, Field(min_length=1, max_length=128)]


class RefreshRequest(BaseModel):
    refresh_token: Annotated[str, Field(min_length=1, max_length=2048)]


class ChangePasswordRequest(BaseModel):
    current_password: Annotated[str, Field(min_length=1, max_length=128)]
    new_password: Password


class AuthResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(description="Скільки секунд дійсний access_token")
    refresh_expires_in: int
    user: UserOut

    @classmethod
    def of(cls, tokens: TokenPair) -> "AuthResponse":
        now = datetime.now(UTC)
        return cls(
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            expires_in=int((tokens.access_expires_at - now).total_seconds()),
            refresh_expires_in=int((tokens.refresh_expires_at - now).total_seconds()),
            user=UserOut.of(tokens.user),
        )


@router.post(
    "/auth/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Реєстрація",
    description="Одразу повертає токени: окремий вхід після реєстрації не потрібен.",
)
async def register_user(body: RegisterRequest, session: SessionDep) -> AuthResponse:
    try:
        tokens = await register(session, body.username, str(body.email), body.password)
    except UserExists as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return AuthResponse.of(tokens)


@router.post(
    "/auth/login",
    response_model=AuthResponse,
    summary="Вхід за username або email",
    description=(
        "access_token передається в заголовку Authorization: Bearer <токен>. Коли він "
        "закінчиться (401), нову пару дає /auth/refresh. Після 10 невдалих спроб за 15 хвилин "
        "вхід для цього логіна тимчасово блокується (429)."
    ),
)
async def login_user(body: LoginRequest, session: SessionDep) -> AuthResponse:
    try:
        tokens = await login(session, body.login, body.password)
    except InvalidCredentials as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    except AccountBlocked as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except TooManyAttempts as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    return AuthResponse.of(tokens)


@router.post(
    "/auth/refresh",
    response_model=AuthResponse,
    summary="Нова пара токенів за refresh-токеном",
    description=(
        "Refresh-токен одноразовий: після обміну старий недійсний. Повторне використання "
        "вже обміняного токена закриває всі сесії користувача."
    ),
)
async def refresh_tokens(body: RefreshRequest, session: SessionDep) -> AuthResponse:
    try:
        tokens = await refresh(session, body.refresh_token)
    except InvalidRefreshToken as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    except AccountBlocked as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    return AuthResponse.of(tokens)


@router.post(
    "/auth/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Вихід: відкликає refresh-токен",
)
async def logout_user(body: RefreshRequest, session: SessionDep) -> None:
    await logout(session, body.refresh_token)


@router.post(
    "/auth/change-password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Зміна пароля",
    description="Усі сесії закриваються: на інших пристроях доведеться увійти знову.",
)
async def change_user_password(
    body: ChangePasswordRequest, user: CurrentUser, session: SessionDep
) -> None:
    try:
        await change_password(session, user, body.current_password, body.new_password)
    except InvalidCredentials as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


class GoogleExchangeRequest(BaseModel):
    code: Annotated[str, Field(min_length=1, max_length=200)]


def _to_frontend(origin: str, path: str, **params: str) -> RedirectResponse:
    return RedirectResponse(f"{origin}{path}?{urlencode(params)}", status.HTTP_302_FOUND)


@router.get(
    "/auth/google/start",
    response_class=RedirectResponse,
    status_code=status.HTTP_302_FOUND,
    summary="Вхід через Google: перехід на вибір облікового запису",
    description=(
        "Відкривати в браузері (посилання або window.location), не через fetch. Перекидає на "
        "Google з вибором облікового запису; після вибору браузер повертається на фронтенд "
        "/auth/google/callback?code=...&next=..., а code обмінюється на токени через "
        "POST /auth/google/exchange. Помилки — /login?error=google_disabled | google_denied | "
        "google_state | google_unverified | account_blocked | google_failed.\n\n"
        "origin — адреса фронтенду, куди повернутись (лише з CORS_ORIGINS); next — шлях на "
        "сайті після входу."
    ),
)
async def google_start(
    origin: str | None = None,
    next_path: Annotated[str | None, Query(alias="next")] = None,
) -> RedirectResponse:
    try:
        url = await start_url(get_redis(), origin, next_path)
    except GoogleAuthError as exc:
        return _to_frontend(allowed_origin(origin), "/login", error=exc.code)
    except (RedisError, OSError):
        return _to_frontend(allowed_origin(origin), "/login", error="google_failed")
    return RedirectResponse(url, status.HTTP_302_FOUND)


@router.get(
    "/auth/google/callback",
    response_class=RedirectResponse,
    status_code=status.HTTP_302_FOUND,
    summary="Повернення з Google (адреса для Google Cloud Console)",
    include_in_schema=False,
)
async def google_callback(
    session: SessionDep,
    state: str = "",
    code: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    try:
        async with httpx.AsyncClient() as client:
            result = await finish(session, get_redis(), client, state, code, error)
    except GoogleAuthError as exc:
        return _to_frontend(exc.origin or allowed_origin(None), "/login", error=exc.code)
    except (RedisError, OSError):
        return _to_frontend(allowed_origin(None), "/login", error="google_failed")
    return _to_frontend(
        result.origin, "/auth/google/callback", code=result.login_code, next=result.next_path
    )


@router.post(
    "/auth/google/exchange",
    response_model=AuthResponse,
    summary="Обмін одноразового коду після входу через Google на токени",
    description="Код живе 60 секунд і спрацьовує один раз.",
)
async def google_exchange(body: GoogleExchangeRequest, session: SessionDep) -> AuthResponse:
    try:
        tokens = await redeem(session, get_redis(), body.code)
    except GoogleAuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    except AccountBlocked as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except (RedisError, OSError) as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Вхід тимчасово недоступний, спробуйте пізніше"
        ) from exc
    return AuthResponse.of(tokens)
