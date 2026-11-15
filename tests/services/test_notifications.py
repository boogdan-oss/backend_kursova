import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.domain import AlertRule, Opportunity, Step
from app.services import alert_service, notify_channels_service
from app.services.alert_service import outside_cooldown, route_key
from app.services.email_service import EmailError
from app.services.notification_templates import (
    PairQuote,
    alert_text,
    email_alert,
    email_code,
    pairs_message,
    telegram_alert,
)
from app.services.notify_channels_service import (
    CODE_KEY,
    ChannelError,
    clear_notify_email,
    confirm_notify_email,
    request_notify_email,
    send_test,
)
from tests.factories import BrokenRedis, FakeRedis

NOW = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)


def rule(**changes: Any) -> AlertRule:
    values: dict[str, Any] = {
        "id": 7,
        "user_id": 1,
        "asset": "USDT",
        "fiat": "UAH",
        "title": "Арбітраж <USDT>",
        "min_spread": Decimal("1"),
        "channels": ["telegram", "email"],
    }
    return AlertRule(**(values | changes))


def opportunity(**changes: Any) -> Opportunity:
    values: dict[str, Any] = {
        "kind": "cross_exchange",
        "asset": "USDT",
        "fiat": "UAH",
        "buy_exchange": "bybit",
        "sell_exchange": "okx",
        "buy_price": Decimal("41.05"),
        "sell_price": Decimal("41.62"),
        "steps": [],
        "spread_pct": Decimal("1.3886"),
        "net_spread_pct": Decimal("1.1"),
        "estimated_profit": Decimal("22000.5"),
        "max_volume": Decimal("1500"),
        "ai_score": 81,
        "rr": Decimal("2.4"),
        "detected_at": NOW,
    }
    return Opportunity(**(values | changes))


def test_telegram_alert_is_html_with_escaped_title() -> None:
    text = telegram_alert(rule(), opportunity())

    assert text.startswith("🔔 <b>Арбітраж &lt;USDT&gt;</b>")
    assert "<b>USDT/UAH</b> · міжбіржовий арбітраж" in text
    assert "Купити: Bybit за 41.0500 UAH" in text
    assert "Продати: OKX за 41.6200 UAH" in text
    assert "прибуток ≈ 22 000.50 UAH" in text
    assert "AI Score 81 · RR 2.40 · обсяг до 1 500.00 USDT" in text


def test_email_alert_has_text_and_html_versions() -> None:
    content = email_alert(rule(), opportunity())

    assert content.subject == "CryptoParser: Арбітраж <USDT> — USDT/UAH 1.10 %"
    assert "Купити: Bybit за 41.0500 UAH" in content.text
    assert "Арбітраж &lt;USDT&gt;" in content.html
    assert "Відкрити арбітраж" in content.html and "/arbitrage" in content.html
    assert alert_text(rule(), opportunity()) == content.text


def test_chain_route_lists_steps() -> None:
    chain = opportunity(
        kind="chain",
        steps=[
            Step(exchange="binance", action="BUY", asset="USDT", fiat="UAH"),
            Step(exchange="htx", action="SELL", asset="USDT", fiat="UAH"),
        ],
    )

    assert "Маршрут: Binance buy USDT → HTX sell USDT" in telegram_alert(rule(), chain)
    assert route_key(chain) == "chain:binance.USDT-htx.USDT"
    assert route_key(opportunity()) == "cross_exchange:bybit:okx"


def test_code_letter_contains_code() -> None:
    content = email_code("042917", 15)

    assert "042917" in content.subject and "042917" in content.text and "042917" in content.html


def test_pairs_message_handles_single_exchange_and_empty_market() -> None:
    quotes = [
        PairQuote("BTC", "EUR", "okx", Decimal("62400"), None, None, None, 4000),
        PairQuote("ETH", "UAH", None, None, None, None, None, None),
    ]

    text = pairs_message(quotes, hidden=2)

    assert "<b>BTC/EUR</b> · OKX" in text and "Купити: 62 400.00 EUR\n" in text
    assert "оновлено 66 хв тому" in text
    assert "Немає надійних оголошень" in text and "даних ще немає" in text
    assert "…і ще 2" in text


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    values = SimpleNamespace(alert_cooldown_min=15, frontend_url="https://site.test")
    monkeypatch.setattr(alert_service, "get_settings", lambda: values)
    return values


async def test_cooldown_blocks_repeat_of_same_route(
    monkeypatch: pytest.MonkeyPatch, settings: SimpleNamespace
) -> None:
    redis = FakeRedis()
    monkeypatch.setattr(alert_service, "get_redis", lambda: redis)

    assert await outside_cooldown(7, opportunity())
    assert not await outside_cooldown(7, opportunity())
    assert await outside_cooldown(7, opportunity(sell_exchange="htx"))
    assert await outside_cooldown(8, opportunity())
    assert redis.expires["alerts:cooldown:7:cross_exchange:bybit:okx"] == 900


async def test_cooldown_off_or_without_redis(
    monkeypatch: pytest.MonkeyPatch, settings: SimpleNamespace
) -> None:
    monkeypatch.setattr(alert_service, "get_redis", lambda: BrokenRedis())
    assert await outside_cooldown(7, opportunity())
    settings.alert_cooldown_min = 0
    assert await outside_cooldown(7, opportunity())


class FakeUser:
    def __init__(self, chat: str | None = "555", notify: str | None = None) -> None:
        self.id = 1
        self.username = "bohdan"
        self.email = "bohdan@example.com"
        self.telegram_chat_id = chat
        self.notify_email = notify


class FakeSession:
    def __init__(self) -> None:
        self.committed = False

    async def commit(self) -> None:
        self.committed = True


@pytest.fixture
def mail(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"sent": [], "enabled": True, "fail": False, "saved": []}

    async def send_email(to: str, subject: str, body: str, html: str | None = None) -> None:
        if state["fail"]:
            raise EmailError("SMTPAuthenticationError")
        state["sent"].append({"to": to, "subject": subject, "body": body, "html": html})

    async def set_notify_email(_: Any, user_id: int, email: str | None) -> None:
        state["saved"].append(email)

    monkeypatch.setattr(notify_channels_service, "send_email", send_email)
    monkeypatch.setattr(notify_channels_service, "email_enabled", lambda: state["enabled"])
    monkeypatch.setattr(notify_channels_service, "set_notify_email", set_notify_email)
    return state


async def test_notify_email_is_saved_only_after_code(mail: dict[str, Any]) -> None:
    redis: Any = FakeRedis()
    user: Any = FakeUser()
    session: Any = FakeSession()

    sent = await request_notify_email(redis, user, " Alerts@Mail.TEST ")
    code = json.loads(redis.values[CODE_KEY.format(user_id=1)])["code"]

    assert (sent.email, sent.expires_in) == ("alerts@mail.test", 900)
    assert mail["sent"][0]["to"] == "alerts@mail.test" and code in mail["sent"][0]["body"]
    assert mail["saved"] == []
    with pytest.raises(ChannelError, match="Невірний код. Залишилось спроб: 4"):
        await confirm_notify_email(session, redis, user, "000000" if code != "000000" else "1")
    await confirm_notify_email(session, redis, user, code)
    assert mail["saved"] == ["alerts@mail.test"] and user.notify_email == "alerts@mail.test"
    assert session.committed
    with pytest.raises(ChannelError, match="застарів"):
        await confirm_notify_email(session, redis, user, code)


async def test_resend_is_throttled_and_attempts_are_limited(mail: dict[str, Any]) -> None:
    redis: Any = FakeRedis()
    user: Any = FakeUser()
    await request_notify_email(redis, user, "a@mail.test")

    with pytest.raises(ChannelError) as again:
        await request_notify_email(redis, user, "a@mail.test")
    assert again.value.kind == "too_often"

    code = json.loads(redis.values[CODE_KEY.format(user_id=1)])["code"]
    wrong = "111111" if code != "111111" else "222222"
    for _ in range(4):
        with pytest.raises(ChannelError, match="Невірний код"):
            await confirm_notify_email(FakeSession(), redis, user, wrong)  # type: ignore[arg-type]
    with pytest.raises(ChannelError, match="Забагато"):
        await confirm_notify_email(FakeSession(), redis, user, wrong)  # type: ignore[arg-type]
    with pytest.raises(ChannelError, match="застарів"):
        await confirm_notify_email(FakeSession(), redis, user, code)  # type: ignore[arg-type]


async def test_account_email_confirmed_again_clears_override(mail: dict[str, Any]) -> None:
    redis: Any = FakeRedis()
    user: Any = FakeUser(notify="old@mail.test")
    await request_notify_email(redis, user, "BOHDAN@example.com")
    code = json.loads(redis.values[CODE_KEY.format(user_id=1)])["code"]

    await confirm_notify_email(FakeSession(), redis, user, code)  # type: ignore[arg-type]

    assert mail["saved"] == [None] and user.notify_email is None
    user.notify_email = "x@mail.test"
    await clear_notify_email(FakeSession(), user)  # type: ignore[arg-type]
    assert user.notify_email is None


async def test_notify_email_errors(mail: dict[str, Any]) -> None:
    mail["enabled"] = False
    with pytest.raises(ChannelError) as disabled:
        await request_notify_email(FakeRedis(), FakeUser(), "a@mail.test")  # type: ignore[arg-type]
    assert disabled.value.kind == "unavailable"

    mail["enabled"], mail["fail"] = True, True
    with pytest.raises(ChannelError) as failed:
        await request_notify_email(FakeRedis(), FakeUser(), "a@mail.test")  # type: ignore[arg-type]
    assert failed.value.kind == "failed"


async def test_test_email_goes_to_notify_address(mail: dict[str, Any]) -> None:
    redis: Any = FakeRedis()

    sent_to = await send_test(redis, FakeUser(notify="alerts@mail.test"), "email")  # type: ignore[arg-type]

    assert sent_to == "alerts@mail.test"
    assert mail["sent"][0]["subject"] == "CryptoParser: тестове сповіщення"
    assert mail["sent"][0]["html"]
    with pytest.raises(ChannelError) as again:
        await send_test(redis, FakeUser(), "email")  # type: ignore[arg-type]
    assert again.value.kind == "too_often"


async def test_test_telegram(monkeypatch: pytest.MonkeyPatch, mail: dict[str, Any]) -> None:
    sent: list[tuple[str, str, bool]] = []

    class Bot:
        async def send_message(
            self, chat_id: str, text: str, html: bool = False, button: Any = None
        ) -> None:
            sent.append((chat_id, text, html))

    monkeypatch.setattr(notify_channels_service, "get_telegram", lambda: Bot())

    assert await send_test(FakeRedis(), FakeUser(), "telegram") == "Telegram"  # type: ignore[arg-type]
    assert sent[0][0] == "555" and sent[0][2] is True and "bohdan" in sent[0][1]
    with pytest.raises(ChannelError, match="підключіть Telegram"):
        await send_test(FakeRedis(), FakeUser(chat=None), "telegram")  # type: ignore[arg-type]
