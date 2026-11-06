import time
from datetime import UTC, datetime
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.domain import CollectorStatus
from app.services.collector_state import (
    STATUS_TTL_SEC,
    WANTED_TTL_SEC,
    publish_status,
    read_status,
    request_pair,
    wanted_pairs,
)

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expires: dict[str, int] = {}
        self.sorted_set: dict[str, float] = {}

    async def set(self, key: str, value: str, ex: int) -> None:
        self.values[key] = value
        self.expires[key] = ex

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def zadd(self, _: str, mapping: dict[str, float]) -> None:
        self.sorted_set.update(mapping)

    async def zremrangebyscore(self, _: str, __: str, deadline: float) -> None:
        self.sorted_set = {k: v for k, v in self.sorted_set.items() if v > deadline}

    async def zrange(self, _: str, __: int, ___: int) -> list[str]:
        return list(self.sorted_set)


class BrokenRedis:
    def __getattr__(self, _: str) -> Any:
        async def fail(*__: Any, **___: Any) -> None:
            raise RedisConnectionError("redis is down")

        return fail


def make_status() -> CollectorStatus:
    return CollectorStatus(
        exchange="binance",
        rps=2.5,
        rps_ceiling=7,
        limiter_state="paused",
        paused_for_sec=4.2,
        tasks={"high": 2},
        requests_ok=10,
        requests_failed=1,
        rows_written=200,
        started_at=NOW,
        updated_at=NOW,
    )


async def test_status_roundtrip() -> None:
    redis: Any = FakeRedis()

    await publish_status(redis, make_status())

    assert await read_status(redis, "binance") == make_status()
    assert list(redis.expires.values()) == [STATUS_TTL_SEC]


async def test_missing_status_means_collector_is_down() -> None:
    assert await read_status(FakeRedis(), "binance") is None  # type: ignore[arg-type]


async def test_corrupted_status_is_ignored() -> None:
    redis: Any = FakeRedis()
    redis.values["collector:status:binance"] = "{not json"

    assert await read_status(redis, "binance") is None


async def test_wanted_pairs_roundtrip() -> None:
    redis: Any = FakeRedis()

    assert await request_pair(redis, "BTC", "EUR") is True
    assert await wanted_pairs(redis) == {("BTC", "EUR")}


async def test_old_requests_expire(monkeypatch: pytest.MonkeyPatch) -> None:
    redis: Any = FakeRedis()
    await request_pair(redis, "BTC", "EUR")
    now = time.time()
    monkeypatch.setattr("app.services.collector_state.time.time", lambda: now + WANTED_TTL_SEC + 1)

    assert await wanted_pairs(redis) == set()


async def test_malformed_wanted_member_is_skipped() -> None:
    redis: Any = FakeRedis()
    redis.sorted_set["broken"] = 9e18

    assert await wanted_pairs(redis) == set()


async def test_redis_outage_never_raises() -> None:
    redis: Any = BrokenRedis()

    await publish_status(redis, make_status())

    assert await read_status(redis, "binance") is None
    assert await request_pair(redis, "BTC", "EUR") is False
    assert await wanted_pairs(redis) == set()
