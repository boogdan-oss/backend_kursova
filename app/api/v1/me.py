from typing import Annotated

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, StringConstraints
from redis.exceptions import RedisError

from app.api.deps import CurrentUser, SessionDep
from app.api.v1.auth import UserOut, UserSettings
from app.db.repositories.users import set_profile, set_telegram_chat
from app.pubsub import get_redis
from app.services.auth_service import update_settings
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
