"""Етап 9 наскрізно на справжній БД: стрічка, публікації, коментарі, профілі, скарги, модерація."""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.session import get_session
from app.main import app

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL не заданий")

TABLES = "users, refresh_tokens, opportunities, market_stats"
PASSWORD = "correct-horse-1"
API = "/api/v1/community"

Headers = dict[str, str]


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
    yield factory
    app.dependency_overrides.clear()
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
    await engine.dispose()


async def sql(factory: async_sessionmaker[AsyncSession], statement: str, **params: Any) -> Any:
    async with factory() as session, session.begin():
        result = await session.execute(text(statement), params)
        return result.scalar() if result.returns_rows else None


async def register(client: AsyncClient, username: str) -> Headers:
    response = await client.post(
        "/api/v1/auth/register",
        json={"username": username, "email": f"{username}@example.com", "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def login(client: AsyncClient, username: str) -> Any:
    return await client.post("/api/v1/auth/login", json={"login": username, "password": PASSWORD})


async def make_admin(
    factory: async_sessionmaker[AsyncSession], client: AsyncClient, username: str
) -> Headers:
    headers = await register(client, username)
    await sql(factory, "UPDATE users SET role = 'admin' WHERE username = :name", name=username)
    return headers


async def user_id(factory: async_sessionmaker[AsyncSession], username: str) -> int:
    found: int = await sql(factory, "SELECT id FROM users WHERE username = :name", name=username)
    return found


async def add_opportunity(factory: async_sessionmaker[AsyncSession]) -> int:
    opportunity_id: int = await sql(
        factory,
        "INSERT INTO opportunities (time, kind, pair_id, buy_exchange_id, sell_exchange_id, "
        "buy_price, sell_price, steps, spread, net_spread, estimated_profit, max_volume, "
        "ai_score, duration_sec) "
        "SELECT now(), 'cross_exchange', p.id, b.id, s.id, 41.05, 41.62, '[]', 1.39, 1.1, "
        "220, 500, 81, 45 FROM currency_pairs p "
        "JOIN assets a ON a.id = p.base_currency_id JOIN fiats f ON f.id = p.quote_currency_id "
        "JOIN exchanges b ON b.code = 'bybit' JOIN exchanges s ON s.code = 'binance' "
        "WHERE a.code = 'USDT' AND f.code = 'UAH' RETURNING id",
    )
    return opportunity_id


async def add_price(
    factory: async_sessionmaker[AsyncSession], at: datetime, exchange: str, price: str
) -> None:
    await sql(
        factory,
        "INSERT INTO market_stats (time, exchange_id, pair_id, buy_price, volume, "
        "offers_count_buy, offers_count_sell) "
        "SELECT :at, e.id, p.id, CAST(:price AS numeric), 100, 3, 3 FROM currency_pairs p "
        "JOIN assets a ON a.id = p.base_currency_id JOIN fiats f ON f.id = p.quote_currency_id "
        "JOIN exchanges e ON e.code = :exchange WHERE a.code = 'USDT' AND f.code = 'UAH'",
        at=at,
        exchange=exchange,
        price=price,
    )


async def create_post(client: AsyncClient, headers: Headers, **body: Any) -> dict[str, Any]:
    response = await client.post(f"{API}/posts", json=body, headers=headers)
    assert response.status_code == 201, response.text
    post: dict[str, Any] = response.json()
    return post


async def test_feed_likes_reposts_comments_and_profile(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    alice = await register(client, "alice")
    bob = await register(client, "bob")
    opportunity_id = await add_opportunity(session_factory)

    first = await create_post(client, alice, text="  Перша думка  ")
    post = await create_post(
        client,
        alice,
        text="USDT/UAH між Bybit і Binance",
        pair={"asset": "usdt", "fiat": "uah"},
        opportunity_id=opportunity_id,
    )
    assert first["text"] == "Перша думка"
    assert post["pair"] == {"asset": "USDT", "fiat": "UAH"}
    attached = post["opportunity"]
    assert (attached["buy_exchange"], attached["sell_exchange"]) == ("bybit", "binance")
    assert (attached["buy_price"], attached["sell_price"]) == ("41.05000000", "41.62000000")
    assert post["author"] == {"id": post["author"]["id"], "username": "alice", "is_admin": False}

    liked = await client.put(f"{API}/posts/{post['id']}/like", headers=bob)
    again = await client.put(f"{API}/posts/{post['id']}/like", headers=bob)
    reposted = await client.put(f"{API}/posts/{post['id']}/repost", headers=bob)
    comment = await client.post(
        f"{API}/posts/{post['id']}/comments", json={"text": "Згоден"}, headers=bob
    )
    assert liked.json()["likes"] == again.json()["likes"] == 1
    assert liked.json()["liked_by_me"] is True
    assert reposted.json()["reposts"] == 1 and reposted.json()["reposted_by_me"] is True
    assert comment.status_code == 201
    own_repost = await client.put(f"{API}/posts/{post['id']}/repost", headers=alice)
    assert own_repost.status_code == 400

    feed = (await client.get(f"{API}/feed")).json()
    kinds = [
        (item["post"]["id"], (item["reposted_by"] or {}).get("username")) for item in feed["items"]
    ]
    assert kinds == [(post["id"], "bob"), (first["id"], None)]
    assert feed["next_cursor"] is None
    anonymous = feed["items"][0]["post"]
    assert (anonymous["likes"], anonymous["comments"], anonymous["reposts"]) == (1, 1, 1)
    assert anonymous["liked_by_me"] is False

    pages, cursor = [], None
    while True:
        params = {"limit": 1} | ({"cursor": cursor} if cursor else {})
        page = (await client.get(f"{API}/feed", params=params)).json()
        pages += [(item["post"]["id"], item["reposted_by"] is not None) for item in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert pages == [(post["id"], True), (first["id"], False)]

    popular = (await client.get(f"{API}/feed", params={"sort": "popular"}, headers=bob)).json()
    assert [item["post"]["id"] for item in popular["items"]] == [post["id"], first["id"]]
    assert popular["items"][0]["post"]["liked_by_me"] is True

    mine = (await client.get(f"{API}/feed", params={"scope": "mine"}, headers=bob)).json()
    assert [item["reposted_by"]["username"] for item in mine["items"]] == ["bob"]
    assert (await client.get(f"{API}/feed", params={"scope": "mine"})).status_code == 401
    by_pair = (await client.get(f"{API}/feed", params={"asset": "USDT", "fiat": "UAH"})).json()
    assert {item["post"]["id"] for item in by_pair["items"]} == {post["id"]}
    bad_cursor = await client.get(f"{API}/feed", params={"cursor": "nonsense"})
    assert bad_cursor.status_code == 400

    profile = (await client.get(f"{API}/users/ALICE")).json()
    assert (profile["posts"], profile["likes_received"], profile["reposts_received"]) == (2, 1, 1)
    assert "email" not in profile and profile["ban_until"] is None
    assert (await client.get(f"{API}/users/bob")).json()["comments"] == 1
    tabs = {
        tab: (await client.get(f"{API}/users/bob/feed", params={"tab": tab})).json()["items"]
        for tab in ("posts", "reposts", "likes")
    }
    assert tabs["posts"] == []
    assert [item["post"]["id"] for item in tabs["reposts"]] == [post["id"]]
    assert [item["post"]["id"] for item in tabs["likes"]] == [post["id"]]
    assert (await client.get(f"{API}/users/nobody")).status_code == 404

    unliked = await client.delete(f"{API}/posts/{post['id']}/like", headers=bob)
    unreposted = await client.delete(f"{API}/posts/{post['id']}/repost", headers=bob)
    assert unliked.json()["likes"] == 0 and unreposted.json()["reposts"] == 0

    now = datetime.now(UTC)
    for exchange, start, end in (("binance", "40.00", "40.40"), ("bybit", "41.00", "41.00")):
        await add_price(
            session_factory, now - timedelta(hours=24) + timedelta(minutes=1), exchange, start
        )
        await add_price(session_factory, now - timedelta(minutes=1), exchange, end)
    trending = (await client.get(f"{API}/trending-pairs")).json()["items"]
    assert trending == [
        {
            "asset": "USDT",
            "fiat": "UAH",
            "posts": 1,
            "likes": 0,
            "comments": 1,
            "change_pct": "0.50",
        }
    ]


async def test_only_author_edits_and_deletes(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    alice = await register(client, "alice")
    bob = await register(client, "bob")
    post = await create_post(client, alice, text="Текст", pair={"asset": "USDT", "fiat": "UAH"})
    assert post["updated_at"] is None

    assert (await client.post(f"{API}/posts", json={"text": "x"})).status_code == 401
    assert (
        await client.post(f"{API}/posts", json={"text": "   "}, headers=alice)
    ).status_code == 422
    too_long = await client.post(f"{API}/posts", json={"text": "x" * 2001}, headers=alice)
    assert too_long.status_code == 422
    unknown_pair = await client.post(
        f"{API}/posts", json={"text": "x", "pair": {"asset": "ZZZ", "fiat": "UAH"}}, headers=alice
    )
    assert unknown_pair.status_code == 404
    missing = await client.post(
        f"{API}/posts", json={"text": "x", "opportunity_id": 999}, headers=alice
    )
    assert missing.status_code == 404

    foreign = await client.patch(f"{API}/posts/{post['id']}", json={"text": "чуже"}, headers=bob)
    assert foreign.status_code == 403
    edited = await client.patch(f"{API}/posts/{post['id']}", json={"pair": None}, headers=alice)
    assert edited.json()["pair"] is None and edited.json()["text"] == "Текст"
    assert edited.json()["updated_at"] is not None
    null_text = await client.patch(f"{API}/posts/{post['id']}", json={"text": None}, headers=alice)
    assert null_text.status_code == 422

    comment = (
        await client.post(f"{API}/posts/{post['id']}/comments", json={"text": "Мій"}, headers=bob)
    ).json()
    url = f"{API}/comments/{comment['id']}"
    assert (await client.patch(url, json={"text": "Чужий"}, headers=alice)).status_code == 403
    changed = await client.patch(url, json={"text": "Виправив"}, headers=bob)
    assert changed.json()["text"] == "Виправив" and changed.json()["updated_at"] is not None
    assert (await client.delete(url, headers=alice)).status_code == 403
    assert (await client.delete(url, headers=bob)).status_code == 204
    listed = (await client.get(f"{API}/posts/{post['id']}/comments")).json()
    assert listed == {"count": 0, "items": []}

    assert (await client.delete(f"{API}/posts/{post['id']}", headers=bob)).status_code == 403
    assert (await client.delete(f"{API}/posts/{post['id']}", headers=alice)).status_code == 204
    assert (await client.get(f"{API}/posts/{post['id']}")).status_code == 404
    assert (await client.get(f"{API}/feed")).json()["items"] == []
    status = await sql(session_factory, "SELECT status FROM posts WHERE id = :id", id=post["id"])
    assert status == "deleted"


async def test_reports_and_moderation(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    alice = await register(client, "alice")
    bob = await register(client, "bob")
    carol = await register(client, "carol")
    admin = await make_admin(session_factory, client, "moder")
    post = await create_post(client, alice, text="Гарантований прибуток 50 %!")
    comment = (
        await client.post(f"{API}/posts/{post['id']}/comments", json={"text": "спам"}, headers=bob)
    ).json()

    report = await client.post(
        f"{API}/reports",
        json={"post_id": post["id"], "reason": "scam", "details": " обіцяє прибуток "},
        headers=bob,
    )
    assert report.status_code == 201, report.text
    assert report.json()["details"] == "обіцяє прибуток"
    assert report.json()["target_author"]["username"] == "alice"
    duplicate = await client.post(
        f"{API}/reports", json={"post_id": post["id"], "reason": "spam"}, headers=bob
    )
    assert duplicate.status_code == 409
    own = await client.post(
        f"{API}/reports", json={"post_id": post["id"], "reason": "spam"}, headers=alice
    )
    assert own.status_code == 400
    both = await client.post(
        f"{API}/reports",
        json={"post_id": post["id"], "comment_id": comment["id"], "reason": "spam"},
        headers=carol,
    )
    assert both.status_code == 422
    second = await client.post(
        f"{API}/reports", json={"post_id": post["id"], "reason": "spam"}, headers=carol
    )
    on_comment = await client.post(
        f"{API}/reports", json={"comment_id": comment["id"], "reason": "abuse"}, headers=alice
    )
    assert second.status_code == on_comment.status_code == 201
    assert on_comment.json()["post_id"] == post["id"]

    assert (await client.get("/api/v1/admin/stats", headers=bob)).status_code == 403
    stats = (await client.get("/api/v1/admin/stats", headers=admin)).json()
    assert stats == {
        "reports_new": 3,
        "reports_pending": 3,
        "hidden": 0,
        "deleted_7d": 0,
        "banned": 0,
    }
    pending = (
        await client.get("/api/v1/admin/reports", params={"status": "pending"}, headers=admin)
    ).json()
    assert pending["count"] == 3

    hidden = await client.post(
        f"/api/v1/admin/reports/{report.json()['id']}/resolve",
        json={"action": "hide"},
        headers=admin,
    )
    assert hidden.json()["status"] == "resolved"
    assert hidden.json()["target_status"] == "hidden"
    again = await client.post(
        f"/api/v1/admin/reports/{second.json()['id']}/resolve",
        json={"action": "dismiss"},
        headers=admin,
    )
    assert again.status_code == 409
    assert (await client.get(f"{API}/posts/{post['id']}")).status_code == 404
    assert (await client.get(f"{API}/posts/{post['id']}", headers=admin)).json()[
        "status"
    ] == "hidden"
    assert (await client.get(f"{API}/feed")).json()["items"] == []
    listed = (await client.get("/api/v1/admin/hidden", headers=admin)).json()
    assert [item["id"] for item in listed["posts"]] == [post["id"]]

    restored = await client.put(
        f"/api/v1/admin/posts/{post['id']}/status", json={"status": "visible"}, headers=admin
    )
    assert restored.json()["status"] == "visible"
    assert restored.json()["updated_at"] is None
    assert (await client.get(f"{API}/posts/{post['id']}")).status_code == 200

    deleted = await client.post(
        f"/api/v1/admin/reports/{on_comment.json()['id']}/resolve",
        json={"action": "delete"},
        headers=admin,
    )
    assert deleted.json()["target_status"] == "deleted"
    stats = (await client.get("/api/v1/admin/stats", headers=admin)).json()
    assert stats == {
        "reports_new": 0,
        "reports_pending": 0,
        "hidden": 0,
        "deleted_7d": 1,
        "banned": 0,
    }


async def test_bans_block_access_and_expire(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    bob = await register(client, "bob")
    admin = await make_admin(session_factory, client, "moder")
    bob_id = await user_id(session_factory, "bob")
    admin_id = await user_id(session_factory, "moder")

    assert (
        await client.post(
            "/api/v1/admin/bans", json={"user_id": bob_id, "days": 1, "reason": "x"}, headers=bob
        )
    ).status_code == 403
    self_ban = await client.post(
        "/api/v1/admin/bans", json={"user_id": admin_id, "reason": "x"}, headers=admin
    )
    assert self_ban.status_code == 400

    banned = await client.post(
        "/api/v1/admin/bans",
        json={"user_id": bob_id, "days": 3, "reason": "Спам"},
        headers=admin,
    )
    assert banned.status_code == 201, banned.text
    assert banned.json()["user"]["username"] == "bob"
    blocked = await client.post(f"{API}/posts", json={"text": "Привіт"}, headers=bob)
    assert blocked.status_code == 403
    assert "заблоковано до" in blocked.json()["detail"] and "Спам" in blocked.json()["detail"]
    assert (await login(client, "bob")).status_code == 403
    profile = (await client.get(f"{API}/users/bob", headers=admin)).json()
    assert profile["status"] == "suspended" and profile["ban_reason"] == "Спам"
    assert (await client.get(f"{API}/users/bob")).json()["ban_until"] is None
    stats = (await client.get("/api/v1/admin/stats", headers=admin)).json()
    assert stats["banned"] == 1

    await sql(
        session_factory,
        "UPDATE user_bans SET until = now() - interval '1 minute' WHERE user_id = :id",
        id=bob_id,
    )
    assert (await login(client, "bob")).status_code == 200
    assert (
        await sql(session_factory, "SELECT status FROM users WHERE id = :id", id=bob_id) == "active"
    )

    forever = await client.post(
        "/api/v1/admin/bans",
        json={"user_id": bob_id, "days": None, "reason": "Шахрайство"},
        headers=admin,
    )
    assert forever.json()["until"] is None
    response = await login(client, "bob")
    assert response.status_code == 403 and "назавжди" in response.json()["detail"]
    active = (await client.get("/api/v1/admin/bans", headers=admin)).json()
    assert [item["reason"] for item in active["items"]] == ["Шахрайство"]
    every = (await client.get("/api/v1/admin/bans", params={"active": False}, headers=admin)).json()
    assert every["count"] == 2

    assert (await client.delete(f"/api/v1/admin/bans/{bob_id}", headers=admin)).status_code == 204
    assert (await login(client, "bob")).status_code == 200
    assert (await client.get("/api/v1/admin/bans", headers=admin)).json()["count"] == 0
