import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest

from app.collectors.base import ExchangeBlocked, ExchangeError
from app.collectors.binance import BinanceAdapter
from app.collectors.ratelimit import AdaptiveRateLimiter
from app.collectors.scheduler import CollectTask
from app.core.settings import Settings
from app.db.repositories.market import MarketRow
from app.services.collect_service import MarketStatsTracker
from app.services.opportunity_service import OpportunityScanner
from app.services.storage_service import StorageGuard
from app.workers import collector as collector_module
from app.workers.collector import ExchangeCollector, parse_pairs, run_every


def test_parse_pairs() -> None:
    assert parse_pairs(["usdt/uah", "BTC/EUR", "broken"]) == {("USDT", "UAH"), ("BTC", "EUR")}


@pytest.fixture
async def collector() -> AsyncIterator[ExchangeCollector]:
    adapter = BinanceAdapter(AdaptiveRateLimiter(1000, 1000, 60))
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql+asyncpg://test:test@127.0.0.1:1/test",
        redis_url="redis://127.0.0.1:1/0",
        collector_min_refresh_sec=0,
    )
    tracker = MarketStatsTracker()
    scanner = OpportunityScanner(tracker, set())
    storage = StorageGuard(budget_bytes=10**12)
    yield ExchangeCollector(adapter, settings, asyncio.Event(), tracker, scanner, storage)
    await adapter.aclose()


async def run_worker(
    collector: ExchangeCollector,
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[int | Exception],
) -> list[CollectTask]:
    """Прокручує один воркер, доки не закінчаться заготовлені результати збору."""
    seen: list[CollectTask] = []
    stop = collector._stop

    async def fake_collect(_: Any, task: CollectTask, __: Any) -> int:
        seen.append(task)
        outcome = outcomes.pop(0)
        if not outcomes:
            stop.set()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(collector_module, "collect_task", fake_collect)
    monkeypatch.setattr(collector_module, "FAILURE_PAUSE_SEC", 0)
    collector.scheduler.add_pairs([("USDT", "UAH"), ("BTC", "UAH")])
    await asyncio.wait_for(collector._worker(), timeout=5)
    return seen


async def test_worker_counts_successful_collections(
    collector: ExchangeCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = await run_worker(collector, monkeypatch, [20, 15])

    assert len(seen) == 2
    assert collector.counters.requests_ok == 2
    assert collector.counters.rows_written == 35


async def test_worker_survives_exchange_failures(
    collector: ExchangeCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes: list[int | Exception] = [
        ExchangeBlocked("binance", "429"),
        ExchangeError("binance", "bad"),
        20,
    ]

    await run_worker(collector, monkeypatch, outcomes)

    assert collector.counters.requests_failed == 2
    assert collector.counters.requests_ok == 1


async def test_worker_survives_database_failure(
    collector: ExchangeCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    await run_worker(collector, monkeypatch, [ConnectionRefusedError("db is down"), 20])

    assert collector.counters.requests_failed == 1
    assert collector.counters.requests_ok == 1


async def test_failed_task_returns_to_queue(
    collector: ExchangeCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    await run_worker(collector, monkeypatch, [ExchangeError("binance", "bad")])

    assert collector.scheduler._in_flight == set()


async def test_worker_pauses_while_storage_is_full(
    collector: ExchangeCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[CollectTask] = []

    async def fake_collect(_: Any, task: CollectTask, __: Any) -> int:
        calls.append(task)
        return 20

    async def stop_on_pause(stop: asyncio.Event, _: float) -> None:
        stop.set()

    collector._storage.full = True
    collector.scheduler.add_pairs([("USDT", "UAH")])
    monkeypatch.setattr(collector_module, "collect_task", fake_collect)
    monkeypatch.setattr(collector_module, "pause", stop_on_pause)

    await asyncio.wait_for(collector._worker(), timeout=5)

    assert calls == []


async def test_periodic_step_survives_exception(collector: ExchangeCollector) -> None:
    calls = 0

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            collector._stop.set()
        raise RuntimeError("boom")

    await asyncio.wait_for(run_every(collector._stop, 0, flaky), timeout=5)

    assert calls == 2


class FakeSession:
    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    def begin(self) -> "FakeSession":
        return self


async def test_restart_continues_round_from_oldest_pairs(
    collector: ExchangeCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    def market(asset: str, fetched_at: datetime | None) -> MarketRow:
        return MarketRow(1, "binance", 1, asset, "UAH", [], fetched_at, fetched_at)

    async def fake_markets(*_: Any) -> list[MarketRow]:
        return [
            market("FRESH", datetime(2026, 11, 6, 12, tzinfo=UTC)),
            market("OLD", datetime(2026, 11, 6, 9, tzinfo=UTC)),
            market("NEVER", None),
        ]

    async def fake_listed_at(*_: Any) -> None:
        return None

    monkeypatch.setattr(collector_module, "SessionLocal", FakeSession)
    monkeypatch.setattr(collector_module, "find_markets", fake_markets)
    monkeypatch.setattr(collector_module, "markets_listed_at", fake_listed_at)

    await collector._load_known_pairs()

    order = []
    while (task := collector.scheduler.next_task()) is not None:
        order.append(task.asset)
    assert list(dict.fromkeys(order)) == ["NEVER", "OLD", "FRESH"]


async def test_exchange_status_follows_cooldown(
    collector: ExchangeCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    published: list[Any] = []
    saved: list[Any] = []

    async def fake_publish(_: Any, status: Any) -> None:
        published.append(status)

    async def fake_set_status(_: Any, exchange: str, status: Any) -> None:
        saved.append((exchange, status.value))

    monkeypatch.setattr(collector_module, "publish_status", fake_publish)
    monkeypatch.setattr(collector_module, "set_exchange_status", fake_set_status)
    monkeypatch.setattr(collector_module, "SessionLocal", FakeSession)
    monkeypatch.setattr(collector_module, "get_redis", lambda: None)

    await collector.report_status()
    assert saved == [("binance", "active")]

    await collector.report_status()
    assert saved == [("binance", "active")]

    limiter = collector.adapter.limiter
    limiter._in_cooldown = True
    limiter._paused_until = float("inf")
    await collector.report_status()
    assert saved[-1] == ("binance", "degraded")

    limiter._paused_until = 0.0
    await collector.report_status()
    assert saved[-1] == ("binance", "active")
    assert published[-1].limiter_state == "ok"
