"""Канали сповіщень користувача: окрема пошта з підтвердженням кодом і тестові повідомлення.

Без підтвердженої пошти листи йдуть на email акаунта. Нова адреса стає активною лише після
коду з листа на неї: так сповіщення не можна спрямувати на чужу скриньку.
"""

import json
import secrets
from dataclasses import dataclass
from typing import Literal

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.db.repositories.users import set_notify_email
from app.services.email_service import EmailError, email_enabled, send_email
from app.services.notification_templates import email_code, email_test, site_url, telegram_test
from app.services.telegram_service import TelegramDisabled, TelegramError, get_telegram

CODE_KEY = "notify:email:code:{user_id}"
RESEND_KEY = "notify:email:resend:{user_id}"
TEST_KEY = "notify:test:{user_id}:{channel}"
CODE_TTL_SEC = 900
RESEND_SEC = 60
TEST_PAUSE_SEC = 30
MAX_ATTEMPTS = 5

Channel = Literal["telegram", "email"]

log = structlog.get_logger()


class ChannelError(Exception):
    """Текст — для користувача; kind визначає HTTP-статус в API."""

    def __init__(
        self,
        message: str,
        kind: Literal["invalid", "unavailable", "too_often", "failed"] = "invalid",
    ) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True, slots=True)
class CodeSent:
    email: str
    expires_in: int


def mail_to(user: User) -> str:
    return user.notify_email or user.email


async def _once(redis: Redis, key: str, seconds: int) -> bool:
    """True — дію можна робити зараз (і пауза почалась); без Redis — завжди можна."""
    try:
        return bool(await redis.set(key, "1", ex=seconds, nx=True))
    except (RedisError, OSError):
        return True


async def request_notify_email(redis: Redis, user: User, email: str) -> CodeSent:
    """Надсилає 6-значний код на нову адресу. Не частіше ніж раз на хвилину."""
    if not email_enabled():
        raise ChannelError("Пошта на сервері не налаштована", "unavailable")
    email = email.strip().lower()
    if not await _once(redis, RESEND_KEY.format(user_id=user.id), RESEND_SEC):
        raise ChannelError("Код уже надіслано, новий можна запросити за хвилину", "too_often")
    code = f"{secrets.randbelow(10**6):06d}"
    payload = json.dumps({"email": email, "code": code, "attempts": 0})
    try:
        await redis.set(CODE_KEY.format(user_id=user.id), payload, ex=CODE_TTL_SEC)
    except (RedisError, OSError) as exc:
        raise ChannelError("Підтвердження зараз недоступне, спробуйте пізніше", "failed") from exc
    content = email_code(code, CODE_TTL_SEC // 60)
    try:
        await send_email(email, content.subject, content.text, content.html)
    except EmailError as exc:
        log.warning("notify.code_failed", user_id=user.id, error=str(exc))
        raise ChannelError("Не вдалося надіслати лист на цю адресу", "failed") from exc
    log.info("notify.code_sent", user_id=user.id)
    return CodeSent(email, CODE_TTL_SEC)


async def confirm_notify_email(session: AsyncSession, redis: Redis, user: User, code: str) -> None:
    key = CODE_KEY.format(user_id=user.id)
    try:
        raw = await redis.get(key)
    except (RedisError, OSError) as exc:
        raise ChannelError("Підтвердження зараз недоступне, спробуйте пізніше", "failed") from exc
    if raw is None:
        raise ChannelError("Код застарів або не запитувався. Надішліть новий")
    saved = json.loads(raw)
    if not secrets.compare_digest(str(saved["code"]), code.strip()):
        attempts = int(saved.get("attempts", 0)) + 1
        if attempts >= MAX_ATTEMPTS:
            await redis.delete(key)
            raise ChannelError("Забагато невдалих спроб. Надішліть новий код")
        await redis.set(key, json.dumps({**saved, "attempts": attempts}), ex=CODE_TTL_SEC)
        raise ChannelError(f"Невірний код. Залишилось спроб: {MAX_ATTEMPTS - attempts}")
    await redis.delete(key)
    email = str(saved["email"])
    value = None if email == user.email.lower() else email
    await set_notify_email(session, user.id, value)
    await session.commit()
    user.notify_email = value
    log.info("notify.email_confirmed", user_id=user.id)


async def clear_notify_email(session: AsyncSession, user: User) -> None:
    await set_notify_email(session, user.id, None)
    await session.commit()
    user.notify_email = None


async def send_test(redis: Redis, user: User, channel: Channel) -> str:
    """Тестове повідомлення в канал. Повертає, куди надіслано."""
    if channel == "telegram":
        if not user.telegram_chat_id:
            raise ChannelError("Спершу підключіть Telegram у профілі")
        try:
            bot = get_telegram()
        except TelegramDisabled as exc:
            raise ChannelError("Telegram-бот на сервері не налаштований", "unavailable") from exc
    elif not email_enabled():
        raise ChannelError("Пошта на сервері не налаштована", "unavailable")
    if not await _once(redis, TEST_KEY.format(user_id=user.id, channel=channel), TEST_PAUSE_SEC):
        raise ChannelError("Тестове повідомлення можна надсилати раз на 30 секунд", "too_often")
    if channel == "telegram":
        assert user.telegram_chat_id is not None
        url = site_url("/alerts")
        try:
            await bot.send_message(
                user.telegram_chat_id,
                telegram_test(user.username),
                html=True,
                button=("Мої правила", url) if url else None,
            )
        except TelegramError as exc:
            log.warning("notify.test_telegram_failed", error=str(exc))
            raise ChannelError(
                "Telegram не прийняв повідомлення. Перепідключіть бота в профілі", "failed"
            ) from exc
        return "Telegram"
    address = mail_to(user)
    content = email_test(user.username)
    try:
        await send_email(address, content.subject, content.text, content.html)
    except EmailError as exc:
        log.warning("notify.test_email_failed", error=str(exc))
        raise ChannelError(
            "Не вдалося надіслати лист. Перевірте налаштування пошти", "failed"
        ) from exc
    return address
