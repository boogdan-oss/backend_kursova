"""Telegram-бот: надсилання сповіщень і прив'язка чату до акаунта.

Прив'язка: API видає посилання t.me/<бот>?start=<код>; код живе в Redis 10 хвилин.
Користувач натискає Start, бот отримує "/start <код>" і записує chat_id у users.
Повідомлення боту читає лише collector (long polling): два процеси з getUpdates на один
токен Telegram не дозволяє.

У логах ніколи немає URL запиту: токен бота є частиною адреси.
"""

import asyncio
import secrets
from typing import Any

import httpx
import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.db.repositories.users import get_user, set_telegram_chat, unlink_telegram_chat
from app.db.session import SessionLocal

API_URL = "https://api.telegram.org"
LINK_KEY = "telegram:link:{code}"
LINK_TTL_SEC = 600
POLL_TIMEOUT_SEC = 30
RETRY_PAUSE_SEC = 10.0
MAX_MESSAGE_LENGTH = 4000

log = structlog.get_logger()


class TelegramError(Exception):
    """Telegram відповів помилкою або недоступний."""


class TelegramDisabled(Exception):
    """Токен бота не заданий."""


class TelegramClient:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None) -> None:
        self._base = f"{API_URL}/bot{token}"
        self._client = client or httpx.AsyncClient(timeout=POLL_TIMEOUT_SEC + 10)
        self._username: str | None = None

    async def call(self, method: str, **params: Any) -> Any:
        try:
            response = await self._client.post(f"{self._base}/{method}", json=params)
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TelegramError(f"{method}: {type(exc).__name__}") from None
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description', response.status_code)}")
        return body["result"]

    async def username(self) -> str:
        if self._username is None:
            me = await self.call("getMe")
            self._username = str(me["username"])
        return self._username

    async def send_message(self, chat_id: str, text: str) -> None:
        await self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text[:MAX_MESSAGE_LENGTH],
            disable_web_page_preview=True,
        )

    async def get_updates(self, offset: int | None, wait_sec: int = POLL_TIMEOUT_SEC) -> list[Any]:
        params: dict[str, Any] = {"timeout": wait_sec, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        return list(await self.call("getUpdates", **params))

    async def aclose(self) -> None:
        await self._client.aclose()


_client: TelegramClient | None = None


def get_telegram() -> TelegramClient:
    """Спільний клієнт процесу. Без токена — TelegramDisabled."""
    global _client
    token = get_settings().telegram_bot_token
    if not token:
        raise TelegramDisabled("Telegram-бот не налаштований: задайте TELEGRAM_BOT_TOKEN")
    if _client is None:
        _client = TelegramClient(token)
    return _client


async def create_link(redis: Redis, user_id: int) -> str:
    """Одноразовий код для /start. RedisError — прив'язка зараз неможлива."""
    code = secrets.token_urlsafe(12)
    await redis.set(LINK_KEY.format(code=code), str(user_id), ex=LINK_TTL_SEC)
    return code


async def _take_link(redis: Redis, code: str) -> int | None:
    try:
        value = await redis.getdel(LINK_KEY.format(code=code))
    except (RedisError, OSError) as exc:
        log.warning("telegram.link_read_failed", error=repr(exc))
        return None
    return int(value) if value else None


async def handle_update(
    update: dict[str, Any],
    client: TelegramClient,
    redis: Redis,
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
) -> None:
    """Команди бота: /start <код> прив'язує чат, /stop відв'язує, решта — підказка."""
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    text = str(message.get("text") or "").strip()
    if chat.get("type") != "private" or "id" not in chat:
        return
    chat_id = str(chat["id"])
    command, _, argument = text.partition(" ")
    command = command.split("@", 1)[0]

    if command == "/start" and argument:
        user_id = await _take_link(redis, argument.strip())
        if user_id is None:
            reply = "Посилання застаріло або вже використане. Створіть нове в профілі CryptoParser."
        else:
            async with session_factory() as session, session.begin():
                user = await get_user(session, user_id)
                if user is not None:
                    await set_telegram_chat(session, user_id, chat_id)
            reply = (
                f"Telegram підключено до акаунта {user.username}. Сюди приходитимуть сповіщення."
                if user is not None
                else "Акаунт не знайдено. Створіть нове посилання в профілі CryptoParser."
            )
            log.info("telegram.linked", user_id=user_id, ok=user is not None)
    elif command == "/stop":
        async with session_factory() as session, session.begin():
            unlinked = await unlink_telegram_chat(session, chat_id)
        reply = (
            "Сповіщення в Telegram вимкнено. Підключити знову можна в профілі CryptoParser."
            if unlinked
            else "Цей чат не підключений до жодного акаунта."
        )
    else:
        reply = (
            "Це бот сповіщень CryptoParser. Щоб підключити його, відкрийте профіль на сайті "
            "й натисніть «Підключити Telegram». /stop — вимкнути сповіщення."
        )
    try:
        await client.send_message(chat_id, reply)
    except TelegramError as exc:
        log.warning("telegram.reply_failed", error=str(exc))


async def poll_updates(
    stop: asyncio.Event,
    client: TelegramClient,
    redis_factory: Any,
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
) -> None:
    """Читає повідомлення боту до зупинки collector. Збій не зупиняє цикл."""
    offset: int | None = None
    while not stop.is_set():
        try:
            updates = await client.get_updates(offset)
        except TelegramError as exc:
            log.warning("telegram.poll_failed", error=str(exc))
            await _pause(stop, RETRY_PAUSE_SEC)
            continue
        for update in updates:
            offset = int(update["update_id"]) + 1
            try:
                await handle_update(update, client, redis_factory(), session_factory)
            except Exception as exc:
                log.error("telegram.update_failed", error=repr(exc))


async def _pause(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except TimeoutError:
        pass
