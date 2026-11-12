import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import respx

from app.services import telegram_service
from app.services.telegram_service import (
    LINK_KEY,
    LINK_TTL_SEC,
    TelegramClient,
    TelegramError,
    create_link,
    handle_update,
    poll_updates,
)
from tests.factories import BrokenRedis, FakeRedis

TOKEN = "123456:SECRET-TOKEN"
BASE = f"https://api.telegram.org/bot{TOKEN}"


class FakeSession:
    @asynccontextmanager
    async def begin(self) -> AsyncIterator[None]:
        yield


@asynccontextmanager
async def fake_session_factory() -> AsyncIterator[FakeSession]:
    yield FakeSession()


class FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.username = f"user{user_id}"


class Bot:
    """Підміна TelegramClient: записує відповіді бота."""

    def __init__(self, updates: list[Any] | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.updates = updates or []
        self.fail_polls = 0

    async def send_message(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))

    async def get_updates(self, offset: int | None) -> list[Any]:
        await asyncio.sleep(0)
        if self.fail_polls:
            self.fail_polls -= 1
            raise TelegramError("getUpdates: ConnectError")
        pending = [item for item in self.updates if offset is None or item["update_id"] >= offset]
        self.updates = []
        return pending


@pytest.fixture
def users(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"chats": {}, "existing": {5}, "linked": asyncio.Event()}

    async def get_user(_: Any, user_id: int) -> FakeUser | None:
        return FakeUser(user_id) if user_id in state["existing"] else None

    async def set_telegram_chat(_: Any, user_id: int, chat_id: str | None) -> None:
        state["chats"][user_id] = chat_id
        state["linked"].set()

    async def unlink_telegram_chat(_: Any, chat_id: str) -> int:
        linked = [uid for uid, chat in state["chats"].items() if chat == chat_id]
        for uid in linked:
            state["chats"][uid] = None
        return len(linked)

    monkeypatch.setattr(telegram_service, "get_user", get_user)
    monkeypatch.setattr(telegram_service, "set_telegram_chat", set_telegram_chat)
    monkeypatch.setattr(telegram_service, "unlink_telegram_chat", unlink_telegram_chat)
    return state


def message(text: str, chat_id: int = 777, chat_type: str = "private", update_id: int = 1) -> Any:
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id, "type": chat_type}, "text": text},
    }


async def handle(update: Any, bot: Bot, redis: Any) -> None:
    await handle_update(update, bot, redis, fake_session_factory)  # type: ignore[arg-type]


async def test_link_code_lives_ten_minutes() -> None:
    redis: Any = FakeRedis()

    code = await create_link(redis, 5)

    assert redis.values[LINK_KEY.format(code=code)] == "5"
    assert redis.expires[LINK_KEY.format(code=code)] == LINK_TTL_SEC


async def test_start_with_code_links_chat_once(users: dict[str, Any]) -> None:
    redis: Any = FakeRedis()
    bot = Bot()
    code = await create_link(redis, 5)

    await handle(message(f"/start {code}"), bot, redis)
    await handle(message(f"/start {code}"), bot, redis)

    assert users["chats"] == {5: "777"}
    assert "підключено до акаунта user5" in bot.sent[0][1]
    assert "застаріло" in bot.sent[1][1]


async def test_code_of_deleted_user_does_not_link(users: dict[str, Any]) -> None:
    redis: Any = FakeRedis()
    bot = Bot()
    code = await create_link(redis, 99)

    await handle(message(f"/start {code}"), bot, redis)

    assert users["chats"] == {}
    assert "не знайдено" in bot.sent[0][1]


async def test_stop_unlinks_chat(users: dict[str, Any]) -> None:
    users["chats"][5] = "777"
    bot = Bot()

    await handle(message("/stop"), bot, FakeRedis())

    assert users["chats"][5] is None
    assert "вимкнено" in bot.sent[0][1]


async def test_other_text_gets_help(users: dict[str, Any]) -> None:
    bot = Bot()

    await handle(message("привіт"), bot, FakeRedis())

    assert "Підключити Telegram" in bot.sent[0][1]


async def test_group_chats_are_ignored(users: dict[str, Any]) -> None:
    bot = Bot()

    await handle(message("/start abc", chat_type="group"), bot, FakeRedis())

    assert bot.sent == []


async def test_redis_outage_means_expired_link(users: dict[str, Any]) -> None:
    bot = Bot()

    await handle(message("/start abc"), bot, BrokenRedis())

    assert users["chats"] == {}
    assert "застаріло" in bot.sent[0][1]


async def test_polling_survives_errors_and_moves_offset(
    users: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(telegram_service, "RETRY_PAUSE_SEC", 0)
    redis = FakeRedis()
    code = await create_link(redis, 5)  # type: ignore[arg-type]
    bot = Bot([message("привіт", update_id=10), message(f"/start {code}", update_id=11)])
    bot.fail_polls = 1
    stop = asyncio.Event()

    async def stop_when_linked() -> None:
        await users["linked"].wait()
        stop.set()

    await asyncio.wait_for(
        asyncio.gather(
            poll_updates(stop, bot, lambda: redis, fake_session_factory),  # type: ignore[arg-type]
            stop_when_linked(),
        ),
        timeout=5,
    )

    assert users["chats"][5] == "777"
    assert len(bot.sent) == 2


@respx.mock
async def test_client_returns_result_and_hides_token_in_errors() -> None:
    respx.post(f"{BASE}/getMe").respond(json={"ok": True, "result": {"username": "upars_bot"}})
    respx.post(f"{BASE}/sendMessage").respond(
        400, json={"ok": False, "description": "Bad Request: chat not found"}
    )
    respx.post(f"{BASE}/getUpdates").mock(side_effect=httpx.ConnectError("boom"))
    client = TelegramClient(TOKEN)

    assert await client.username() == "upars_bot"
    with pytest.raises(TelegramError) as not_found:
        await client.send_message("1", "hi")
    with pytest.raises(TelegramError) as offline:
        await client.get_updates(None)
    await client.aclose()

    assert "chat not found" in str(not_found.value)
    assert "ConnectError" in str(offline.value)
    for error in (not_found.value, offline.value):
        assert "SECRET" not in str(error) and error.__cause__ is None
