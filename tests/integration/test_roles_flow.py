"""Ролі user < admin < owner на справжній БД: доступ, модерація за рангом, блокування, ролі."""

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.session import get_session
from app.main import app

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL не заданий")

Headers = dict[str, str]
ADMIN = "/api/v1/admin"
COMMUNITY = "/api/v1/community"


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


async def register(
    factory: async_sessionmaker[AsyncSession], client: AsyncClient, name: str, role: str = "user"
) -> tuple[int, Headers]:
    response = await client.post(
        "/api/v1/auth/register",
        json={"username": name, "email": f"{name}@example.com", "password": "pass-12345"},
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    if role != "user":
        async with factory() as session, session.begin():
            await session.execute(
                text("UPDATE users SET role = :role WHERE id = :id"),
                {"role": role, "id": body["user"]["id"]},
            )
    return body["user"]["id"], {"Authorization": f"Bearer {body['access_token']}"}


async def post(client: AsyncClient, headers: Headers, text_: str) -> int:
    response = await client.post(f"{COMMUNITY}/posts", json={"text": text_}, headers=headers)
    assert response.status_code == 201, response.text
    post_id: int = response.json()["id"]
    return post_id


async def test_owner_has_admin_rights_and_roles_are_visible(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    owner_id, owner = await register(session_factory, client, "boss", "owner")
    _, alice = await register(session_factory, client, "alice")

    me = (await client.get("/api/v1/me", headers=owner)).json()
    assert me["role"] == "owner"
    assert (await client.get(f"{ADMIN}/stats", headers=owner)).status_code == 200
    assert (await client.get(f"{ADMIN}/stats", headers=alice)).status_code == 403

    post_id = await post(client, owner, "Правила спільноти")
    author = (await client.get(f"{COMMUNITY}/posts/{post_id}")).json()["author"]
    assert author == {"id": owner_id, "username": "boss", "is_admin": True, "role": "owner"}
    profile = (await client.get(f"{COMMUNITY}/users/boss")).json()
    assert (profile["role"], profile["is_admin"]) == ("owner", True)
    assert (await client.get(f"{COMMUNITY}/users/alice")).json()["role"] == "user"


async def test_moderation_respects_rank(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    _, owner = await register(session_factory, client, "boss", "owner")
    _, admin = await register(session_factory, client, "moder", "admin")
    _, second = await register(session_factory, client, "moder2", "admin")
    _, alice = await register(session_factory, client, "alice")

    owner_post = await post(client, owner, "Пост власника")
    admin_post = await post(client, second, "Пост іншого адміна")
    own_post = await post(client, admin, "Мій пост")
    user_post = await post(client, alice, "Пост користувача")

    def status_url(post_id: int) -> str:
        return f"{ADMIN}/posts/{post_id}/status"

    hidden = {"status": "hidden"}
    for target in (owner_post, admin_post):
        response = await client.put(status_url(target), json=hidden, headers=admin)
        assert response.status_code == 403, response.text
    for target in (own_post, user_post):
        assert (await client.put(status_url(target), json=hidden, headers=admin)).status_code == 200
    assert (await client.put(status_url(admin_post), json=hidden, headers=owner)).status_code == 200
    restored = await client.put(status_url(admin_post), json={"status": "visible"}, headers=admin)
    assert restored.status_code == 200

    comment = await client.post(
        f"{COMMUNITY}/posts/{user_post}/comments", json={"text": "Від власника"}, headers=owner
    )
    assert comment.status_code == 404
    await client.put(status_url(user_post), json={"status": "visible"}, headers=admin)
    comment = await client.post(
        f"{COMMUNITY}/posts/{user_post}/comments", json={"text": "Від власника"}, headers=owner
    )
    comment_id = comment.json()["id"]
    report = await client.post(
        f"{COMMUNITY}/reports", json={"comment_id": comment_id, "reason": "spam"}, headers=alice
    )
    resolved = await client.post(
        f"{ADMIN}/reports/{report.json()['id']}/resolve", json={"action": "hide"}, headers=admin
    )
    assert resolved.status_code == 403
    dismissed = await client.post(
        f"{ADMIN}/reports/{report.json()['id']}/resolve",
        json={"action": "dismiss"},
        headers=admin,
    )
    assert dismissed.status_code == 200


async def test_bans_follow_rank(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    owner_id, owner = await register(session_factory, client, "boss", "owner")
    admin_id, admin = await register(session_factory, client, "moder", "admin")
    second_id, _ = await register(session_factory, client, "moder2", "admin")
    alice_id, _ = await register(session_factory, client, "alice")

    def ban(user_id: int) -> dict[str, Any]:
        return {"user_id": user_id, "days": 1, "reason": "Порушення"}

    for target in (owner_id, second_id, admin_id):
        response = await client.post(f"{ADMIN}/bans", json=ban(target), headers=admin)
        assert response.status_code == 400, response.text
    assert (
        await client.post(f"{ADMIN}/bans", json=ban(alice_id), headers=admin)
    ).status_code == 201
    assert (
        await client.post(f"{ADMIN}/bans", json=ban(owner_id), headers=owner)
    ).status_code == 400

    banned_admin = await client.post(f"{ADMIN}/bans", json=ban(second_id), headers=owner)
    assert banned_admin.status_code == 201
    assert (await client.delete(f"{ADMIN}/bans/{second_id}", headers=admin)).status_code == 400
    assert (await client.delete(f"{ADMIN}/bans/{second_id}", headers=owner)).status_code == 204
    assert (await client.delete(f"{ADMIN}/bans/{alice_id}", headers=admin)).status_code == 204


async def test_owner_manages_roles(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    owner_id, owner = await register(session_factory, client, "boss", "owner")
    _, admin = await register(session_factory, client, "moder", "admin")
    alice_id, alice = await register(session_factory, client, "alice")
    await post(client, alice, "Перший пост")
    report_post = await post(client, alice, "Сумнівний пост")
    await client.post(
        f"{COMMUNITY}/reports", json={"post_id": report_post, "reason": "scam"}, headers=admin
    )

    listed = (await client.get(f"{ADMIN}/users", headers=admin)).json()
    assert [item["username"] for item in listed["items"]] == ["alice", "moder", "boss"]
    found = (await client.get(f"{ADMIN}/users", params={"query": "ALI"}, headers=admin)).json()
    assert found["count"] == 1
    row = found["items"][0]
    assert (row["role"], row["posts"], row["reports_against"]) == ("user", 2, 1)
    admins = (await client.get(f"{ADMIN}/users", params={"role": "admin"}, headers=owner)).json()
    assert [item["username"] for item in admins["items"]] == ["moder"]
    assert (await client.get(f"{ADMIN}/users", headers=alice)).status_code == 403

    url = f"{ADMIN}/users/{alice_id}/role"
    assert (await client.put(url, json={"role": "admin"}, headers=admin)).status_code == 403
    assert (await client.put(url, json={"role": "owner"}, headers=owner)).status_code == 422
    promoted = await client.put(url, json={"role": "admin"}, headers=owner)
    assert promoted.status_code == 200 and promoted.json()["role"] == "admin"
    assert (await client.get(f"{ADMIN}/stats", headers=alice)).status_code == 200
    demoted = await client.put(url, json={"role": "user"}, headers=owner)
    assert demoted.json()["role"] == "user"
    assert (await client.get(f"{ADMIN}/stats", headers=alice)).status_code == 403

    own = await client.put(f"{ADMIN}/users/{owner_id}/role", json={"role": "user"}, headers=owner)
    assert own.status_code == 400
    missing = await client.put(f"{ADMIN}/users/999999/role", json={"role": "user"}, headers=owner)
    assert missing.status_code == 404
