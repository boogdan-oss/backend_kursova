"""Вхід через Google наскрізно на справжній БД; Google підмінено respx."""

import os
import time
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.settings import Settings
from app.db.models import User, UserStatus
from app.db.session import get_session
from app.main import app
from app.services import google_auth_service
from app.services.google_auth_service import TOKEN_URL
from tests.factories import FakeRedis

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL не заданий")

CLIENT_ID = "123.apps.googleusercontent.com"
FRONTEND = "https://kursovafrontend.vercel.app"


@pytest.fixture
async def session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE users, refresh_tokens CASCADE"))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def test_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=DATABASE_URL,
        redis_url="redis://localhost:1/0",
        google_client_id=CLIENT_ID,
        google_client_secret="secret",
        frontend_url=FRONTEND,
        cors_origins=[FRONTEND, "http://localhost:5173"],
    )
    monkeypatch.setattr(google_auth_service, "get_settings", lambda: settings)
    app.dependency_overrides[get_session] = test_session
    yield factory
    app.dependency_overrides.clear()
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE users, refresh_tokens CASCADE"))
    await engine.dispose()


def google_answers(sub: str = "g-1", email: str = "bohdan@gmail.com", **claims: Any) -> None:
    token = jwt.encode(
        {
            "iss": "accounts.google.com",
            "aud": CLIENT_ID,
            "sub": sub,
            "email": email,
            "email_verified": True,
            "exp": int(time.time()) + 600,
            **claims,
        },
        "test-key-signature-is-not-checked-here",
        algorithm="HS256",
    )
    respx.post(TOKEN_URL).respond(json={"id_token": token, "access_token": "x"})


def query(location: str) -> dict[str, str]:
    return {key: value[0] for key, value in parse_qs(urlparse(location).query).items()}


async def sign_in(client: AsyncClient, origin: str | None = None, next_path: str = "/") -> str:
    """Проходить start → Google → callback. Повертає адресу, куди API відправив браузер."""
    params = {"next": next_path, **({"origin": origin} if origin else {})}
    start = await client.get("/api/v1/auth/google/start", params=params)
    assert start.status_code == 302
    state = query(start.headers["location"])["state"]
    callback = await client.get(
        "/api/v1/auth/google/callback", params={"state": state, "code": "google-code"}
    )
    assert callback.status_code == 302
    return callback.headers["location"]


@respx.mock
async def test_new_user_registers_with_google(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient, redis: FakeRedis
) -> None:
    google_answers()

    location = await sign_in(client, "http://localhost:5173", "/alerts")

    assert location.startswith("http://localhost:5173/auth/google/callback?")
    params = query(location)
    assert params["next"] == "/alerts"
    exchanged = await client.post("/api/v1/auth/google/exchange", json={"code": params["code"]})
    reused = await client.post("/api/v1/auth/google/exchange", json={"code": params["code"]})
    assert exchanged.status_code == 200 and reused.status_code == 401
    user = exchanged.json()["user"]
    assert (user["username"], user["email"], user["google_connected"]) == (
        "bohdan",
        "bohdan@gmail.com",
        True,
    )
    me = await client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {exchanged.json()['access_token']}"}
    )
    assert me.status_code == 200
    sent = respx.calls.last.request
    assert b"code_verifier=" in sent.content and b"client_secret=secret" in sent.content


@respx.mock
async def test_same_google_account_is_same_user(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    google_answers()
    first = query(await sign_in(client))["code"]
    second = query(await sign_in(client))["code"]

    a = await client.post("/api/v1/auth/google/exchange", json={"code": first})
    b = await client.post("/api/v1/auth/google/exchange", json={"code": second})

    assert a.json()["user"]["id"] == b.json()["user"]["id"]
    async with session_factory() as session:
        assert len((await session.execute(select(User))).scalars().all()) == 1


@respx.mock
async def test_existing_email_account_is_linked(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    registered = await client.post(
        "/api/v1/auth/register",
        json={"username": "bohdan", "email": "bohdan@gmail.com", "password": "password-123"},
    )
    google_answers(email="Bohdan@Gmail.com")

    code = query(await sign_in(client))["code"]
    signed = await client.post("/api/v1/auth/google/exchange", json={"code": code})

    assert signed.json()["user"]["id"] == registered.json()["user"]["id"]
    assert signed.json()["user"]["google_connected"] is True
    password_login = await client.post(
        "/api/v1/auth/login", json={"login": "bohdan", "password": "password-123"}
    )
    assert password_login.status_code == 200


@respx.mock
async def test_taken_username_gets_suffix(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    await client.post(
        "/api/v1/auth/register",
        json={"username": "bohdan", "email": "other@example.com", "password": "password-123"},
    )
    google_answers(email="bohdan@gmail.com")

    code = query(await sign_in(client))["code"]
    user = (await client.post("/api/v1/auth/google/exchange", json={"code": code})).json()["user"]

    assert user["username"].startswith("bohdan_") and user["email"] == "bohdan@gmail.com"


@respx.mock
async def test_google_user_cannot_log_in_with_password(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    google_answers()
    await client.post(
        "/api/v1/auth/google/exchange", json={"code": query(await sign_in(client))["code"]}
    )

    for password in ("!google", "", "anything-123"):
        login = await client.post(
            "/api/v1/auth/login", json={"login": "bohdan", "password": password or "x"}
        )
        assert login.status_code == 401


async def test_cancelled_and_forged_returns_go_to_login(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    start = await client.get("/api/v1/auth/google/start", params={"origin": "https://evil.com"})
    state = query(start.headers["location"])["state"]

    cancelled = await client.get(
        "/api/v1/auth/google/callback", params={"state": state, "error": "access_denied"}
    )
    forged = await client.get(
        "/api/v1/auth/google/callback", params={"state": "made-up", "code": "x"}
    )

    assert cancelled.headers["location"] == f"{FRONTEND}/login?error=google_denied"
    assert forged.headers["location"] == f"{FRONTEND}/login?error=google_state"


@respx.mock
async def test_google_outage_and_blocked_user(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("down"))
    assert (await sign_in(client)).endswith("/login?error=google_failed")

    google_answers()
    await client.post(
        "/api/v1/auth/google/exchange", json={"code": query(await sign_in(client))["code"]}
    )
    async with session_factory() as session, session.begin():
        await session.execute(update(User).values(status=UserStatus.BANNED))

    assert (await sign_in(client)).endswith("/login?error=account_blocked")
