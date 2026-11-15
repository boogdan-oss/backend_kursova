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
        self.options: list[dict[str, Any]] = []
        self.updates = updates or []
        self.fail_polls = 0
        self.commands_set = False

    async def send_message(
        self, chat_id: str, text: str, html: bool = False, button: Any = None
    ) -> None:
        self.sent.append((chat_id, text))
        self.options.append({"html": html, "button": button})

    async def set_commands(self) -> None:
        self.commands_set = True

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
    await handle_update(update, bot, redis, fake_session_factory)


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

    assert "/pairs" in bot.sent[0][1] and "/rules" in bot.sent[0][1]


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
    code = await create_link(redis, 5)
    bot = Bot([message("привіт", update_id=10), message(f"/start {code}", update_id=11)])
    bot.fail_polls = 1
    stop = asyncio.Event()

    async def stop_when_linked() -> None:
        await users["linked"].wait()
        stop.set()

    await asyncio.wait_for(
        asyncio.gather(
            poll_updates(stop, bot, lambda: redis, fake_session_factory),
            stop_when_linked(),
        ),
        timeout=5,
    )

    assert users["chats"][5] == "777"
    assert len(bot.sent) == 2
    assert bot.commands_set


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


@pytest.fixture
def linked(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Чат 777 прив'язаний до користувача 5; пари й правила підмінені."""
    from datetime import UTC, datetime
    from decimal import Decimal

    from app.domain import AlertRule
    from app.services.notification_templates import PairQuote

    state: dict[str, Any] = {"quotes": [], "rules": []}

    async def find_user_by_chat(_: Any, chat_id: str) -> FakeUser | None:
        return FakeUser(5) if chat_id == "777" else None

    async def tracked_quotes(_: Any, user_id: int) -> tuple[list[Any], int]:
        return state["quotes"], 0

    async def list_rules(_: Any, user_id: int, __: Any) -> list[Any]:
        return state["rules"]

    monkeypatch.setattr(telegram_service, "find_user_by_chat", find_user_by_chat)
    monkeypatch.setattr(telegram_service, "tracked_quotes", tracked_quotes)
    monkeypatch.setattr(telegram_service, "list_rules", list_rules)
    state["quote"] = PairQuote(
        asset="USDT",
        fiat="UAH",
        exchange=None,
        buy_price=Decimal("41.05"),
        buy_exchange="bybit",
        sell_price=Decimal("41.62"),
        sell_exchange="okx",
        age_sec=12,
    )
    rule = AlertRule(
        id=3,
        user_id=5,
        asset="USDT",
        fiat="UAH",
        exchange="binance",
        title="<b>Мій</b> USDT",
        min_spread=Decimal("1.5"),
        min_ai_score=80,
        channels=["telegram", "email"],
    )
    state["rule_row"] = type(
        "Row", (), {"rule": rule, "last_triggered_at": datetime(2026, 11, 15, 9, 30, tzinfo=UTC)}
    )
    return state


async def test_pairs_shows_best_prices_of_tracked_pairs(linked: dict[str, Any]) -> None:
    bot = Bot()
    linked["quotes"] = [linked["quote"]]

    await handle(message("/pairs"), bot, FakeRedis())

    text = bot.sent[0][1]
    assert "<b>USDT/UAH</b>" in text
    assert "Купити: 41.0500 UAH — Bybit" in text
    assert "Продати: 41.6200 UAH — OKX" in text
    assert "Спред 1.39 %" in text and "оновлено 12 с тому" in text
    assert bot.options[0]["html"] is True
    assert bot.options[0]["button"][0] == "Відкрити на сайті"


async def test_pairs_without_tracked_pairs_explains_how_to_add(linked: dict[str, Any]) -> None:
    bot = Bot()

    await handle(message("/watchlist"), bot, FakeRedis())

    assert "ще немає відстежуваних пар" in bot.sent[0][1]


async def test_rules_lists_rules_and_escapes_titles(linked: dict[str, Any]) -> None:
    bot = Bot()
    linked["rules"] = [linked["rule_row"]]

    await handle(message("/rules"), bot, FakeRedis())

    text = bot.sent[0][1]
    assert "&lt;b&gt;Мій&lt;/b&gt; USDT" in text
    assert "USDT/UAH · Binance: спред ≥ 1.50 %, AI ≥ 80" in text
    assert "Канали: Telegram, пошта" in text and "востаннє 15.11 09:30 UTC" in text


async def test_commands_need_linked_chat(linked: dict[str, Any]) -> None:
    bot = Bot()

    await handle(message("/pairs", chat_id=111), bot, FakeRedis())

    assert "не підключений" in bot.sent[0][1]


@respx.mock
async def test_client_sends_html_with_button_and_sets_commands() -> None:
    sent = respx.post(f"{BASE}/sendMessage").respond(json={"ok": True, "result": {}})
    commands = respx.post(f"{BASE}/setMyCommands").respond(json={"ok": True, "result": True})
    client = TelegramClient(TOKEN)

    await client.send_message("1", "<b>hi</b>", html=True, button=("Open", "https://x.test/a"))
    await client.send_message("1", "plain", button=("Local", "http://localhost:5173"))
    await client.set_commands()
    await client.aclose()

    import json as json_module

    first = json_module.loads(sent.calls[0].request.content)
    second = json_module.loads(sent.calls[1].request.content)
    assert first["parse_mode"] == "HTML"
    assert first["reply_markup"] == {
        "inline_keyboard": [[{"text": "Open", "url": "https://x.test/a"}]]
    }
    assert "parse_mode" not in second and "reply_markup" not in second
    names = [
        item["command"] for item in json_module.loads(commands.calls[0].request.content)["commands"]
    ]
    assert names == ["pairs", "rules", "help", "stop"]
