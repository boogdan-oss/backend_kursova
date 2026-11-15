from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, EmailStr, Field, StringConstraints
from redis.exceptions import RedisError

from app.api.deps import CurrentUser, SessionDep
from app.api.v1.auth import UserOut, UserSettings
from app.db.repositories.users import set_profile, set_telegram_chat
from app.pubsub import get_redis
from app.services.auth_service import update_settings
from app.services.notify_channels_service import (
    ChannelError,
    clear_notify_email,
    confirm_notify_email,
    request_notify_email,
    send_test,
)
from app.services.telegram_service import (
    LINK_TTL_SEC,
    TelegramDisabled,
    TelegramError,
    create_link,
    get_telegram,
)

router = APIRouter(tags=["me"])


class TelegramLinkResponse(BaseModel):
    url: str
    expires_in: int


@router.get("/me", response_model=UserOut, summary="Профіль поточного користувача")
async def me(user: CurrentUser) -> UserOut:
    return UserOut.of(user)


class ProfilePatch(BaseModel):
    """Змінюються лише передані поля; null або порожній рядок очищає поле."""

    display_name: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=64)] = (
        None
    )
    bio: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=300)] = None


@router.patch("/me", response_model=UserOut, summary="Змінити ім'я й опис профілю")
async def patch_profile(body: ProfilePatch, user: CurrentUser, session: SessionDep) -> UserOut:
    changes = {name: (getattr(body, name) or None) for name in body.model_fields_set}
    if changes:
        await set_profile(session, user.id, changes)
        await session.commit()
        for name, value in changes.items():
            setattr(user, name, value)
    return UserOut.of(user)


@router.patch(
    "/me/settings",
    response_model=UserOut,
    summary="Змінити персональні налаштування",
    description="Змінюються лише передані поля; null прибирає поле.",
)
async def patch_settings(body: UserSettings, user: CurrentUser, session: SessionDep) -> UserOut:
    changes = body.model_dump(mode="json", exclude_unset=True)
    for key in ("default_asset", "default_fiat"):
        if changes.get(key):
            changes[key] = changes[key].upper()
    await update_settings(session, user, changes)
    return UserOut.of(user)


@router.post(
    "/me/telegram",
    response_model=TelegramLinkResponse,
    summary="Посилання для підключення Telegram",
    description=(
        "Відкрийте посилання й натисніть Start у боті: чат прив'яжеться до акаунта. "
        "Посилання одноразове і дійсне 10 хвилин."
    ),
)
async def telegram_link(user: CurrentUser) -> TelegramLinkResponse:
    try:
        bot = get_telegram()
        username = await bot.username()
        code = await create_link(get_redis(), user.id)
    except TelegramDisabled as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except (TelegramError, RedisError, OSError) as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Telegram зараз недоступний, спробуйте пізніше"
        ) from exc
    return TelegramLinkResponse(
        url=f"https://t.me/{username}?start={code}", expires_in=LINK_TTL_SEC
    )


@router.delete(
    "/me/telegram",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Відключити Telegram",
)
async def telegram_unlink(user: CurrentUser, session: SessionDep) -> None:
    await set_telegram_chat(session, user.id, None)
    await session.commit()


class NotifyEmailRequest(BaseModel):
    email: EmailStr


class NotifyEmailCode(BaseModel):
    email: str
    expires_in: int = Field(description="Скільки секунд дійсний код")


class NotifyEmailConfirm(BaseModel):
    code: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\d{6}$")]


class TestNotificationRequest(BaseModel):
    channel: Literal["telegram", "email"]


class TestNotificationResponse(BaseModel):
    channel: Literal["telegram", "email"]
    sent_to: str


CHANNEL_STATUS = {
    "invalid": status.HTTP_400_BAD_REQUEST,
    "unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "too_often": status.HTTP_429_TOO_MANY_REQUESTS,
    "failed": status.HTTP_502_BAD_GATEWAY,
}


def _channel_error(exc: ChannelError) -> HTTPException:
    return HTTPException(CHANNEL_STATUS[exc.kind], str(exc))


@router.post(
    "/me/notify-email",
    response_model=NotifyEmailCode,
    summary="Пошта для сповіщень: надіслати код підтвердження",
    description=(
        "На вказану адресу йде 6-значний код (дійсний 15 хвилин); адреса стає активною лише "
        "після POST /me/notify-email/confirm. Новий код — не частіше ніж раз на хвилину (429). "
        "Без пошти на сервері — 503."
    ),
)
async def notify_email_request(body: NotifyEmailRequest, user: CurrentUser) -> NotifyEmailCode:
    try:
        sent = await request_notify_email(get_redis(), user, body.email)
    except ChannelError as exc:
        raise _channel_error(exc) from exc
    return NotifyEmailCode(email=sent.email, expires_in=sent.expires_in)


@router.post(
    "/me/notify-email/confirm",
    response_model=UserOut,
    summary="Пошта для сповіщень: підтвердити кодом",
    description="Невірний чи застарілий код — 400; після 5 невдалих спроб код анулюється.",
)
async def notify_email_confirm(
    body: NotifyEmailConfirm, user: CurrentUser, session: SessionDep
) -> UserOut:
    try:
        await confirm_notify_email(session, get_redis(), user, body.code)
    except ChannelError as exc:
        raise _channel_error(exc) from exc
    return UserOut.of(user)


@router.delete(
    "/me/notify-email",
    response_model=UserOut,
    summary="Пошта для сповіщень: повернути email акаунта",
)
async def notify_email_clear(user: CurrentUser, session: SessionDep) -> UserOut:
    await clear_notify_email(session, user)
    return UserOut.of(user)


@router.post(
    "/me/notifications/test",
    response_model=TestNotificationResponse,
    summary="Тестове сповіщення в Telegram чи на пошту",
    description=(
        "Перевірка, що канал працює. Раз на 30 секунд на канал (429). Telegram не підключений — "
        "400; бот чи пошта на сервері не налаштовані — 503; канал не прийняв повідомлення — 502."
    ),
)
async def notification_test(
    body: TestNotificationRequest, user: CurrentUser
) -> TestNotificationResponse:
    try:
        sent_to = await send_test(get_redis(), user, body.channel)
    except ChannelError as exc:
        raise _channel_error(exc) from exc
    return TestNotificationResponse(channel=body.channel, sent_to=sent_to)
