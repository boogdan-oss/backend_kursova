"""Реєстрація, вхід, оновлення токенів, вихід, зміна пароля й налаштування користувача.

Функції самі комітять сесію: зміни мають зберегтися, навіть коли далі піде помилка
(наприклад, відкликання всіх токенів при повторному використанні refresh-токена).
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from redis.exceptions import RedisError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    InvalidToken,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.db.models import User, UserStatus
from app.db.repositories.community import active_ban
from app.db.repositories.users import (
    consume_refresh_token,
    create_user,
    find_user_by_login,
    get_user,
    revoke_refresh_token,
    revoke_user_tokens,
    save_refresh_token,
    set_password,
    set_settings,
    set_status,
    taken_fields,
)
from app.pubsub import get_redis

LOGIN_ATTEMPTS = 10
LOGIN_WINDOW_SEC = 900
FAIL_KEY = "auth:fail:{login}"
KYIV = ZoneInfo("Europe/Kyiv")

log = structlog.get_logger()


class AuthError(Exception):
    """Базова помилка входу; текст — для користувача."""


class InvalidCredentials(AuthError):
    pass


class AccountBlocked(AuthError):
    pass


class TooManyAttempts(AuthError):
    pass


class InvalidRefreshToken(AuthError):
    pass


class UserExists(AuthError):
    def __init__(self, fields: set[str]) -> None:
        names = {"username": "ім'ям", "email": "email"}
        taken = " і ".join(names[field] for field in sorted(fields))
        super().__init__(f"Користувач з таким {taken} вже існує")
        self.fields = fields


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime
    user: User


@lru_cache
def _dummy_hash() -> str:
    return hash_password("timing-equalizer")


def blocked_message(until: datetime | None, reason: str | None) -> str:
    when = (
        "назавжди"
        if until is None
        else f"до {until.astimezone(KYIV).strftime('%d.%m.%Y %H:%M')} (за Києвом)"
    )
    return f"Обліковий запис заблоковано {when}" + (f". Причина: {reason}" if reason else "")


async def ensure_active(session: AsyncSession, user: User) -> None:
    """Пускає лише активних. Прострочене тимчасове блокування знімається тут же (FR-45)."""
    if user.status == UserStatus.ACTIVE:
        return
    ban = await active_ban(session, user.id, datetime.now(UTC))
    if ban is not None:
        raise AccountBlocked(blocked_message(ban.until, ban.reason))
    if user.status == UserStatus.BANNED:
        raise AccountBlocked(blocked_message(None, None))
    await set_status(session, user.id, UserStatus.ACTIVE)
    await session.commit()
    user.status = UserStatus.ACTIVE
    log.info("auth.ban_expired", user_id=user.id)


async def issue_tokens(session: AsyncSession, user: User) -> TokenPair:
    access, access_expires = create_access_token(user.id)
    refresh, jti, refresh_expires = create_refresh_token(user.id)
    await save_refresh_token(session, jti, user.id, refresh_expires)
    return TokenPair(access, access_expires, refresh, refresh_expires, user)


async def register(session: AsyncSession, username: str, email: str, password: str) -> TokenPair:
    email = email.strip().lower()
    taken = await taken_fields(session, username, email)
    if taken:
        raise UserExists(taken)
    password_hash = await asyncio.to_thread(hash_password, password)
    try:
        user = await create_user(session, username, email, password_hash)
        tokens = await issue_tokens(session, user)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise UserExists(await taken_fields(session, username, email) or {"username"}) from None
    log.info("auth.registered", user_id=user.id)
    return tokens


async def _failures(key: str) -> int:
    try:
        value = await get_redis().get(key)
    except (RedisError, OSError):
        return 0
    return int(value) if value else 0


async def _record_failure(key: str) -> None:
    try:
        redis = get_redis()
        await redis.incr(key)
        await redis.expire(key, LOGIN_WINDOW_SEC)
    except (RedisError, OSError) as exc:
        log.warning("auth.fail_counter_failed", error=repr(exc))


async def _reset_failures(key: str) -> None:
    try:
        await get_redis().delete(key)
    except (RedisError, OSError):
        pass


async def login(session: AsyncSession, login_name: str, password: str) -> TokenPair:
    """Вхід за username або email. Після LOGIN_ATTEMPTS невдач — пауза LOGIN_WINDOW_SEC."""
    key = FAIL_KEY.format(login=login_name.strip().lower())
    if await _failures(key) >= LOGIN_ATTEMPTS:
        raise TooManyAttempts("Забагато невдалих спроб входу. Спробуйте через 15 хвилин")
    user = await find_user_by_login(session, login_name)
    stored = user.password_hash if user is not None else _dummy_hash()
    valid = await asyncio.to_thread(verify_password, password, stored)
    if user is None or not valid:
        await _record_failure(key)
        raise InvalidCredentials("Невірний логін або пароль")
    await ensure_active(session, user)
    await _reset_failures(key)
    tokens = await issue_tokens(session, user)
    await session.commit()
    return tokens


async def refresh(session: AsyncSession, refresh_token: str) -> TokenPair:
    """Обмін refresh-токена на нову пару. Старий токен більше не дійсний.

    Повторне використання вже обміняного токена означає, що його могли вкрасти:
    тоді відкликаються всі сесії користувача.
    """
    try:
        claims = decode_token(refresh_token, "refresh")
    except InvalidToken:
        raise InvalidRefreshToken("Сесія недійсна, увійдіть знову") from None
    assert claims.jti is not None
    result = await consume_refresh_token(session, claims.jti, claims.user_id)
    if result == "reused":
        await revoke_user_tokens(session, claims.user_id)
        await session.commit()
        log.warning("auth.refresh_reused", user_id=claims.user_id)
        raise InvalidRefreshToken("Сесія недійсна, увійдіть знову")
    if result == "invalid":
        raise InvalidRefreshToken("Сесія недійсна, увійдіть знову")
    user = await get_user(session, claims.user_id)
    if user is None:
        raise InvalidRefreshToken("Сесія недійсна, увійдіть знову")
    await ensure_active(session, user)
    tokens = await issue_tokens(session, user)
    await session.commit()
    return tokens


async def logout(session: AsyncSession, refresh_token: str) -> None:
    """Відкликає refresh-токен. Недійсний токен — не помилка: вийти можна завжди."""
    try:
        claims = decode_token(refresh_token, "refresh")
    except InvalidToken:
        return
    assert claims.jti is not None
    await revoke_refresh_token(session, claims.jti, claims.user_id)
    await session.commit()


async def change_password(session: AsyncSession, user: User, current: str, new: str) -> None:
    """Після зміни пароля всі refresh-токени відкликаються: інші пристрої увійдуть знову."""
    if not await asyncio.to_thread(verify_password, current, user.password_hash):
        raise InvalidCredentials("Поточний пароль невірний")
    await set_password(session, user.id, await asyncio.to_thread(hash_password, new))
    await revoke_user_tokens(session, user.id)
    await session.commit()
    log.info("auth.password_changed", user_id=user.id)


async def update_settings(
    session: AsyncSession, user: User, changes: dict[str, Any]
) -> dict[str, Any]:
    """Злиття з наявними налаштуваннями; значення None прибирає поле."""
    merged = {**user.settings, **changes}
    settings = {key: value for key, value in merged.items() if value is not None}
    await set_settings(session, user.id, settings)
    await session.commit()
    user.settings = settings
    return settings
