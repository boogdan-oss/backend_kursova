import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from app.domain import SnapshotEvent, SnapshotUpdate
from app.pubsub import MARKET_UPDATES, listen_events, publish_event
from tests.factories import BrokenRedis, FakeRedis

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def make_event() -> SnapshotEvent:
    update = SnapshotUpdate(exchange="binance", asset="USDT", fiat="UAH", items=[])
    return SnapshotEvent(ts=NOW, data=update)


async def test_event_is_published_to_market_updates() -> None:
    redis: Any = FakeRedis()

    await publish_event(redis, make_event())

    [(channel, message)] = redis.published
    assert channel == MARKET_UPDATES
    assert json.loads(message) == {
        "type": "snapshot",
        "ts": "2026-11-10T12:00:00Z",
        "data": {"exchange": "binance", "asset": "USDT", "fiat": "UAH", "items": []},
    }


async def test_publish_without_redis_is_not_an_error() -> None:
    redis: Any = BrokenRedis()

    await publish_event(redis, make_event())


async def test_listen_yields_only_messages() -> None:
    redis: Any = FakeRedis()
    received: list[str] = []

    async def consume() -> None:
        async for message in listen_events(redis):
            received.append(message)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await redis.publish(MARKET_UPDATES, "first")
    await redis.publish("other:channel", "ignored")
    await redis.publish(MARKET_UPDATES, "second")
    await asyncio.sleep(0)
    task.cancel()

    assert received == ["first", "second"]
    await asyncio.gather(task, return_exceptions=True)
    assert redis.channels[MARKET_UPDATES] == []
