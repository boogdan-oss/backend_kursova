"""Сторінка «AI-асистент» на справжній БД: розмови, історія для моделі, приватність, помилки."""

import os
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from google.genai import errors
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.v1 import ai as ai_module
from app.api.v1 import assistant as assistant_module
from app.db.session import get_session
from app.main import app
from app.services.ai_service import ChatEvent, ChatTurn

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL не заданий")

URL = "/api/v1/assistant"


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE users, refresh_tokens CASCADE"))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def test_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = test_session
    yield factory
    app.dependency_overrides.clear()
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE users, refresh_tokens CASCADE"))
    await engine.dispose()


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Підміна моделі: записує, яку розмову отримала, і відповідає заготовкою."""
    state: dict[str, Any] = {"seen": [], "reply": None}

    async def fake_chat(turns: Sequence[ChatTurn]) -> AsyncIterator[ChatEvent]:
        state["seen"].append(list(turns))
        if state["reply"] is not None:
            for event in state["reply"]:
                yield event
            return
        yield ChatEvent("tool", {"name": "get_best_prices", "input": {}})
        yield ChatEvent("tool", {"name": "get_opportunities", "input": {}})
        yield ChatEvent("tool", {"name": "get_best_prices", "input": {}})
        yield ChatEvent("text", {"text": "Найдешевше "})
        yield ChatEvent("text", {"text": "на Bybit."})
        yield ChatEvent("done", {})

    monkeypatch.setattr(assistant_module, "chat", fake_chat)
    monkeypatch.setattr(assistant_module, "assistant_provider", lambda: "gemini")
    return state


async def register(client: AsyncClient, username: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/register",
        json={"username": username, "email": f"{username}@example.com", "password": "pass-12345"},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def test_conversation_is_saved_and_continued(
    session_factory: async_sessionmaker[AsyncSession],
    client: AsyncClient,
    model: dict[str, Any],
) -> None:
    alice = await register(client, "alice")
    question = "Де зараз вигідніше купити USDT на 20 000 гривень через monobank, і що з арбітражем?"

    first = await client.post(
        f"{URL}/chat", json={"message": question, "asset": "usdt", "fiat": "uah"}, headers=alice
    )

    assert first.status_code == 200, first.text
    body = first.json()
    assert body["title"] == question[:59] + "…"
    assert body["user_message"]["content"] == question
    reply = body["message"]
    assert reply["role"] == "assistant" and reply["content"] == "Найдешевше на Bybit."
    assert reply["tool_calls"] == ["get_best_prices", "get_opportunities"]
    assert reply["actions"] == [
        {"label": "Ціни на ринку", "to": "/offers"},
        {"label": "Показати арбітраж", "to": "/arbitrage"},
    ]
    assert model["seen"][0] == [
        ChatTurn("user", f"(Пара, вибрана на сайті: USDT/UAH.)\n\n{question}")
    ]

    conversation_id = body["conversation_id"]
    second = await client.post(
        f"{URL}/chat",
        json={"conversation_id": conversation_id, "message": "А на 50 000?"},
        headers=alice,
    )
    assert second.status_code == 200
    assert second.json()["conversation_id"] == conversation_id
    assert model["seen"][1] == [
        ChatTurn("user", question),
        ChatTurn("assistant", "Найдешевше на Bybit."),
        ChatTurn("user", "А на 50 000?"),
    ]

    listed = (await client.get(f"{URL}/conversations", headers=alice)).json()
    assert listed["count"] == 1 and listed["items"][0]["id"] == conversation_id
    detail = (await client.get(f"{URL}/conversations/{conversation_id}", headers=alice)).json()
    assert [message["role"] for message in detail["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]

    other = (await client.post(f"{URL}/chat", json={"message": "Інше"}, headers=alice)).json()
    listed = (await client.get(f"{URL}/conversations", headers=alice)).json()
    assert [item["id"] for item in listed["items"]] == [other["conversation_id"], conversation_id]

    deleted = await client.delete(f"{URL}/conversations/{conversation_id}", headers=alice)
    assert deleted.status_code == 204
    assert (
        await client.get(f"{URL}/conversations/{conversation_id}", headers=alice)
    ).status_code == 404


async def test_conversations_are_private(
    session_factory: async_sessionmaker[AsyncSession],
    client: AsyncClient,
    model: dict[str, Any],
) -> None:
    alice = await register(client, "alice")
    bob = await register(client, "bob")
    created = (await client.post(f"{URL}/chat", json={"message": "Привіт"}, headers=alice)).json()
    conversation_id = created["conversation_id"]

    assert (await client.get(f"{URL}/conversations", headers=bob)).json()["count"] == 0
    for request in (
        client.get(f"{URL}/conversations/{conversation_id}", headers=bob),
        client.get(f"{URL}/conversations/not-a-uuid", headers=alice),
        client.post(
            f"{URL}/chat", json={"conversation_id": conversation_id, "message": "x"}, headers=bob
        ),
        client.delete(f"{URL}/conversations/{conversation_id}", headers=bob),
    ):
        assert (await request).status_code == 404
    assert (await client.get(f"{URL}/conversations")).status_code == 401
    assert (
        await client.post(f"{URL}/chat", json={"message": "   "}, headers=alice)
    ).status_code == 422


async def test_failed_answer_saves_nothing(
    session_factory: async_sessionmaker[AsyncSession],
    client: AsyncClient,
    model: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alice = await register(client, "alice")
    model["reply"] = [ChatEvent("error", {"detail": "Асистент не може відповісти на це питання"})]

    refused = await client.post(f"{URL}/chat", json={"message": "?"}, headers=alice)

    assert refused.status_code == 502
    assert refused.json()["detail"] == "Асистент не може відповісти на це питання"
    assert (await client.get(f"{URL}/conversations", headers=alice)).json()["count"] == 0

    async def overloaded(turns: Sequence[ChatTurn]) -> AsyncIterator[ChatEvent]:
        raise errors.ServerError(503, {"error": {"message": "busy", "status": "UNAVAILABLE"}})
        yield ChatEvent("done", {})

    monkeypatch.setattr(assistant_module, "chat", overloaded)
    busy = await client.post(f"{URL}/chat", json={"message": "?"}, headers=alice)
    assert busy.status_code == 503
    assert busy.json()["detail"] == "Асистент перевантажений, спробуйте за хвилину"

    class Limited:
        assistant_messages_per_hour = 0

    monkeypatch.setattr(ai_module, "get_settings", lambda: Limited())
    limited = await client.post(f"{URL}/chat", json={"message": "?"}, headers=alice)
    assert limited.status_code == 429
