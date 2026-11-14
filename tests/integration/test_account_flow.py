"""Етап 7 наскрізно на справжній БД: вхід і токени, профіль, обране, правила й сповіщення."""

import json
import os
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Notification, User, UserStatus
from app.db.session import get_session
from app.main import app
from app.pubsub import MARKET_UPDATES
from app.services import auth_service
from app.services.alert_service import AlertDispatcher
from app.services.opportunity_service import OpportunityScanner
from tests.factories import FakeRedis
from tests.integration.test_opportunity_flow import FixedTracker, prepare_pair, profitable_offers

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL не заданий")

TABLES = (
    "users, refresh_tokens, alert_rules, notifications, watchlist, offer_snapshots, "
    "market_stats, spread_history, opportunities, exchange_markets, merchants"
)
PASSWORD = "correct-horse-1"


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def test_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = test_session
    await prepare_pair(factory)
    yield factory
    app.dependency_overrides.clear()
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
    await engine.dispose()


async def register(client: AsyncClient, username: str = "bohdan") -> dict[str, Any]:
    response = await client.post(
        "/api/v1/auth/register",
        json={"username": username, "email": f"{username}@Example.com", "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def bearer(tokens: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def test_register_login_and_profile(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    tokens = await register(client)

    assert tokens["token_type"] == "bearer"
    assert tokens["expires_in"] > 0
    assert tokens["user"]["email"] == "bohdan@example.com"
    assert tokens["user"]["telegram_connected"] is False

    by_email = await client.post(
        "/api/v1/auth/login", json={"login": "BOHDAN@example.com", "password": PASSWORD}
    )
    by_name = await client.post(
        "/api/v1/auth/login", json={"login": "Bohdan", "password": PASSWORD}
    )
    me = await client.get("/api/v1/me", headers=bearer(by_name.json()))

    assert by_email.status_code == by_name.status_code == 200
    assert me.status_code == 200
    assert me.json()["username"] == "bohdan"
    async with session_factory() as session:
        stored = (await session.execute(select(User.password_hash))).scalar_one()
    assert PASSWORD not in stored


async def test_duplicate_account_is_rejected(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    await register(client)

    same_name = await client.post(
        "/api/v1/auth/register",
        json={"username": "BOHDAN", "email": "other@example.com", "password": PASSWORD},
    )
    same_email = await client.post(
        "/api/v1/auth/register",
        json={"username": "other", "email": "bohdan@example.com", "password": PASSWORD},
    )

    assert same_name.status_code == same_email.status_code == 409
    assert "ім'ям" in same_name.json()["detail"]
    assert "email" in same_email.json()["detail"]


@pytest.mark.parametrize(
    "body",
    [
        {"username": "ab", "email": "a@example.com", "password": PASSWORD},
        {"username": "bad name", "email": "a@example.com", "password": PASSWORD},
        {"username": "valid", "email": "not-an-email", "password": PASSWORD},
        {"username": "valid", "email": "a@example.com", "password": "short"},
    ],
)
async def test_registration_validates_input(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient, body: dict[str, Any]
) -> None:
    response = await client.post("/api/v1/auth/register", json=body)

    assert response.status_code == 422


async def test_wrong_password_and_lockout(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    await register(client)
    wrong = {"login": "bohdan", "password": "nope-nope"}

    statuses = [
        (await client.post("/api/v1/auth/login", json=wrong)).status_code
        for _ in range(auth_service.LOGIN_ATTEMPTS)
    ]
    locked = await client.post("/api/v1/auth/login", json={"login": "bohdan", "password": PASSWORD})

    assert statuses == [401] * auth_service.LOGIN_ATTEMPTS
    assert locked.status_code == 429


async def test_refresh_rotates_and_reuse_closes_all_sessions(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    first = await register(client)

    rotated = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
    )
    reused = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
    )
    after_reuse = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": rotated.json()["refresh_token"]}
    )

    assert rotated.status_code == 200
    assert rotated.json()["refresh_token"] != first["refresh_token"]
    assert reused.status_code == 401
    assert after_reuse.status_code == 401


async def test_logout_revokes_refresh_token(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    tokens = await register(client)

    logout = await client.post(
        "/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}
    )
    refresh = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    garbage_logout = await client.post("/api/v1/auth/logout", json={"refresh_token": "garbage"})

    assert logout.status_code == garbage_logout.status_code == 204
    assert refresh.status_code == 401


async def test_change_password(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    tokens = await register(client)
    url = "/api/v1/auth/change-password"

    wrong = await client.post(
        url,
        headers=bearer(tokens),
        json={"current_password": "nope", "new_password": "new-password-2"},
    )
    changed = await client.post(
        url,
        headers=bearer(tokens),
        json={"current_password": PASSWORD, "new_password": "new-password-2"},
    )
    old_refresh = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    old_login = await client.post(
        "/api/v1/auth/login", json={"login": "bohdan", "password": PASSWORD}
    )
    new_login = await client.post(
        "/api/v1/auth/login", json={"login": "bohdan", "password": "new-password-2"}
    )

    assert wrong.status_code == 400
    assert changed.status_code == 204
    assert old_refresh.status_code == 401
    assert old_login.status_code == 401
    assert new_login.status_code == 200


async def test_protected_endpoints_need_valid_active_user(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    tokens = await register(client)

    anonymous = await client.get("/api/v1/me")
    garbage = await client.get("/api/v1/me", headers={"Authorization": "Bearer garbage"})
    refresh_as_access = await client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {tokens['refresh_token']}"}
    )
    async with session_factory() as session, session.begin():
        await session.execute(update(User).values(status=UserStatus.BANNED))
    banned = await client.get("/api/v1/me", headers=bearer(tokens))
    banned_login = await client.post(
        "/api/v1/auth/login", json={"login": "bohdan", "password": PASSWORD}
    )

    assert anonymous.status_code == garbage.status_code == refresh_as_access.status_code == 401
    assert anonymous.headers["www-authenticate"] == "Bearer"
    assert banned.status_code == banned_login.status_code == 403


async def test_settings_are_merged(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    headers = bearer(await register(client))
    url = "/api/v1/me/settings"

    first = await client.patch(url, headers=headers, json={"default_fiat": "eur", "min_rr": "1.5"})
    second = await client.patch(
        url, headers=headers, json={"default_investment": 20000, "min_rr": None}
    )
    invalid = await client.patch(url, headers=headers, json={"default_investment": -1})

    assert first.json()["settings"]["default_fiat"] == "EUR"
    settings = second.json()["settings"]
    assert settings["default_fiat"] == "EUR"
    assert settings["min_rr"] is None
    assert Decimal(settings["default_investment"]) == 20000
    assert invalid.status_code == 422


async def test_watchlist(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    owner = bearer(await register(client))
    stranger = bearer(await register(client, "stranger"))

    added = await client.post(
        "/api/v1/watchlist", headers=owner, json={"asset": "usdt", "fiat": "uah"}
    )
    again = await client.post(
        "/api/v1/watchlist", headers=owner, json={"asset": "USDT", "fiat": "UAH"}
    )
    on_exchange = await client.post(
        "/api/v1/watchlist",
        headers=owner,
        json={"asset": "USDT", "fiat": "UAH", "exchange": "bybit"},
    )
    unknown_pair = await client.post(
        "/api/v1/watchlist", headers=owner, json={"asset": "DOGE", "fiat": "UAH"}
    )
    unknown_exchange = await client.post(
        "/api/v1/watchlist",
        headers=owner,
        json={"asset": "USDT", "fiat": "UAH", "exchange": "nope"},
    )
    foreign_delete = await client.delete(
        f"/api/v1/watchlist/{added.json()['id']}", headers=stranger
    )
    listed = await client.get("/api/v1/watchlist", headers=owner)
    deleted = await client.delete(f"/api/v1/watchlist/{added.json()['id']}", headers=owner)

    assert added.status_code == 201 and again.status_code == 200
    assert again.json()["id"] == added.json()["id"]
    assert on_exchange.status_code == 201 and on_exchange.json()["exchange"] == "bybit"
    assert unknown_pair.status_code == unknown_exchange.status_code == 404
    assert foreign_delete.status_code == 404
    assert [item["exchange"] for item in listed.json()["items"]] == [None, "bybit"]
    assert deleted.status_code == 204


async def test_rule_crud_and_validation(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    owner = bearer(await register(client))
    stranger = bearer(await register(client, "stranger"))
    url = "/api/v1/alerts/rules"

    no_threshold = await client.post(url, headers=owner, json={"asset": "USDT", "fiat": "UAH"})
    telegram_unlinked = await client.post(
        url,
        headers=owner,
        json={"asset": "USDT", "fiat": "UAH", "min_spread": 1, "channels": ["telegram"]},
    )
    created = await client.post(
        url,
        headers=owner,
        json={
            "asset": "usdt",
            "fiat": "uah",
            "title": "Спред > 1.5%",
            "payment": "Mono",
            "min_spread": "1.5",
            "channels": ["in_app", "in_app"],
        },
    )
    rule_id = created.json()["id"]
    patched = await client.patch(
        f"{url}/{rule_id}", headers=owner, json={"min_ai_score": 80, "enabled": False}
    )
    removes_last_threshold = await client.patch(
        f"{url}/{rule_id}", headers=owner, json={"min_spread": None, "min_ai_score": None}
    )
    foreign_patch = await client.patch(f"{url}/{rule_id}", headers=stranger, json={"enabled": True})
    foreign_delete = await client.delete(f"{url}/{rule_id}", headers=stranger)
    listed = await client.get(url, headers=owner)
    deleted = await client.delete(f"{url}/{rule_id}", headers=owner)

    assert no_threshold.status_code == 422
    assert telegram_unlinked.status_code == 422
    assert "Telegram" in telegram_unlinked.json()["detail"]
    assert created.status_code == 201
    body = created.json()
    assert (body["asset"], body["payment"], body["channels"]) == ("USDT", "monobank", ["in_app"])
    assert patched.json()["min_ai_score"] == 80 and patched.json()["enabled"] is False
    assert removes_last_threshold.status_code == 422
    assert foreign_patch.status_code == foreign_delete.status_code == 404
    assert listed.json()["count"] == 1
    assert listed.json()["items"][0]["triggered_today"] == 0
    assert deleted.status_code == 204


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))


async def test_new_opportunity_triggers_matching_rules(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient, redis: FakeRedis
) -> None:
    owner = bearer(await register(client))
    async with session_factory() as session, session.begin():
        await session.execute(update(User).values(telegram_chat_id="777"))
    url = "/api/v1/alerts/rules"
    fires = await client.post(
        url,
        headers=owner,
        json={
            "asset": "USDT",
            "fiat": "UAH",
            "title": "Спред",
            "min_spread": "1.5",
            "channels": ["in_app", "telegram"],
        },
    )
    too_high = await client.post(
        url, headers=owner, json={"asset": "USDT", "fiat": "UAH", "min_spread": "50"}
    )
    assert fires.status_code == too_high.status_code == 201

    telegram = FakeTelegram()
    dispatcher = AlertDispatcher(telegram, session_factory)  # type: ignore[arg-type]
    scanner = OpportunityScanner(
        FixedTracker(profitable_offers()), set(), session_factory, alerts=dispatcher
    )
    await scanner.scan()
    await scanner.scan()
    await dispatcher.drain()

    alerts = [json.loads(raw) for channel, raw in redis.published if channel == MARKET_UPDATES]
    alerts = [event for event in alerts if event["type"] == "alert"]
    assert len(alerts) == 1
    assert alerts[0]["data"]["rule_id"] == fires.json()["id"]
    assert alerts[0]["data"]["opportunity"]["buy_exchange"] == "binance"
    assert [chat for chat, _ in telegram.sent] == ["777"]
    assert "Спред" in telegram.sent[0][1] and "USDT/UAH" in telegram.sent[0][1]

    async with session_factory() as session:
        stored = (await session.execute(select(Notification))).scalar_one()
    assert stored.delivery == {"in_app": "sent", "telegram": "sent"}

    history = await client.get("/api/v1/notifications", headers=owner)
    body = history.json()
    assert (body["count"], body["unread"]) == (1, 1)
    assert body["items"][0]["opportunity"]["sell_exchange"] == "bybit"
    assert body["items"][0]["rule_title"] == "Спред"

    marked = await client.post("/api/v1/notifications/read", headers=owner, json={})
    rules = await client.get(url, headers=owner)
    assert marked.json() == {"updated": 1}
    triggered = {item["id"]: item["triggered_today"] for item in rules.json()["items"]}
    assert triggered == {fires.json()["id"]: 1, too_high.json()["id"]: 0}


async def test_notifications_are_private(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    owner = bearer(await register(client))
    stranger = bearer(await register(client, "stranger"))
    await client.post(
        "/api/v1/alerts/rules",
        headers=owner,
        json={"asset": "USDT", "fiat": "UAH", "min_spread": "1"},
    )
    dispatcher = AlertDispatcher(None, session_factory)
    await OpportunityScanner(
        FixedTracker(profitable_offers()), set(), session_factory, alerts=dispatcher
    ).scan()

    mine = await client.get("/api/v1/notifications", headers=owner)
    theirs = await client.get("/api/v1/notifications", headers=stranger)
    foreign_read = await client.post("/api/v1/notifications/read", headers=stranger, json={})

    assert mine.json()["count"] == 1
    assert theirs.json()["count"] == 0
    assert foreign_read.json() == {"updated": 0}


async def test_profile_name_bio_and_notification_settings(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    tokens = await register(client)
    headers = bearer(tokens)
    assert tokens["user"]["display_name"] is None and tokens["user"]["bio"] is None

    named = await client.patch(
        "/api/v1/me", json={"display_name": "  Богдан  ", "bio": "P2P з 2021"}, headers=headers
    )
    assert named.status_code == 200, named.text
    assert (named.json()["display_name"], named.json()["bio"]) == ("Богдан", "P2P з 2021")

    cleared = await client.patch("/api/v1/me", json={"bio": ""}, headers=headers)
    assert (cleared.json()["display_name"], cleared.json()["bio"]) == ("Богдан", None)
    public = (await client.get("/api/v1/community/users/bohdan")).json()
    assert (public["display_name"], public["bio"]) == ("Богдан", None)

    too_long = await client.patch("/api/v1/me", json={"bio": "x" * 301}, headers=headers)
    assert too_long.status_code == 422
    assert (await client.patch("/api/v1/me", json={"bio": "x"})).status_code == 401

    saved = await client.patch(
        "/api/v1/me/settings",
        json={"notify_comments": False, "notify_likes": True, "language": "en"},
        headers=headers,
    )
    settings = saved.json()["settings"]
    assert (settings["notify_comments"], settings["notify_likes"], settings["language"]) == (
        False,
        True,
        "en",
    )
    bad = await client.patch("/api/v1/me/settings", json={"language": "de"}, headers=headers)
    assert bad.status_code == 422
