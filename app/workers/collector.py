"""Процес збору даних: `python -m app.workers.collector`."""

import asyncio
import signal
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

from app.collectors.base import ExchangeAdapter, ExchangeBlocked, ExchangeError
from app.collectors.registry import aclose_adapters, available_exchanges, get_adapter
from app.collectors.scheduler import CollectScheduler, Pair
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.core.settings import Settings
from app.db.models import ExchangeStatus
from app.db.repositories.market import (
    find_markets,
    markets_listed_at,
    set_exchange_status,
    tracked_pairs,
)
from app.db.session import SessionLocal, engine
from app.domain import CollectorStatus
from app.pubsub import close_redis, get_redis
from app.services.collect_service import MarketStatsTracker, collect_task, refresh_markets
from app.services.collector_state import publish_status, wanted_pairs
from app.services.opportunity_service import OpportunityScanner

IDLE_SEC = 0.1  # усі задачі в роботі або щойно оновлені
FAILURE_PAUSE_SEC = 5.0  # БД недоступна: нема сенсу витрачати запити до біржі
PRIORITIES_INTERVAL_SEC = 2.0
STATUS_INTERVAL_SEC = 5.0
PROGRESS_INTERVAL_SEC = 30.0
MARKETS_CHECK_INTERVAL_SEC = 600.0
MARKETS_MAX_AGE = timedelta(days=1)
OPPORTUNITIES_INTERVAL_SEC = 30.0
NEVER_FETCHED = datetime.min.replace(tzinfo=UTC)
# Від такої кількості оголошень на стороні пара вважається популярною: це повна сторінка
POPULAR_MIN_OFFERS = 20

log = structlog.get_logger()


def parse_pairs(values: list[str]) -> set[Pair]:
    """["USDT/UAH"] → {("USDT", "UAH")}."""
    pairs = (value.upper().split("/", 1) for value in values)
    return {(pair[0], pair[1]) for pair in pairs if len(pair) == 2}


async def pause(stop: asyncio.Event, seconds: float) -> None:
    with suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)


async def run_every(
    stop: asyncio.Event, seconds: float, step: Callable[[], Awaitable[None]]
) -> None:
    """Періодичний крок: збій одного проходу не зупиняє наступні."""
    while not stop.is_set():
        try:
            await step()
        except Exception as exc:
            log.error("collector.step_failed", step=step.__name__, error=repr(exc))
        await pause(stop, seconds)


@dataclass(slots=True)
class CollectorCounters:
    requests_ok: int = 0
    requests_failed: int = 0
    rows_written: int = 0


class ExchangeCollector:
    """Збір однієї біржі. Темп задає rate limiter адаптера, порядок — планувальник."""

    def __init__(
        self,
        adapter: ExchangeAdapter,
        settings: Settings,
        stop: asyncio.Event,
        tracker: MarketStatsTracker,
        scanner: OpportunityScanner,
    ) -> None:
        self.adapter = adapter
        self.scheduler = CollectScheduler(
            adapter.name, settings.collector_min_refresh_sec, POPULAR_MIN_OFFERS
        )
        self.counters = CollectorCounters()
        self._tracker = tracker
        self._scanner = scanner
        self._settings = settings
        self._stop = stop
        self._priority_pairs = parse_pairs(settings.collector_priority_pairs)
        self._started_at = datetime.now(UTC)
        # None: перший звіт перезапише статус, який лишився від попереднього запуску
        self._exchange_status: ExchangeStatus | None = None

    async def run(self) -> None:
        await asyncio.gather(
            *(self._worker() for _ in range(self._settings.collector_concurrency)),
            run_every(self._stop, PRIORITIES_INTERVAL_SEC, self.update_priorities),
            run_every(self._stop, MARKETS_CHECK_INTERVAL_SEC, self.update_markets),
            run_every(self._stop, STATUS_INTERVAL_SEC, self.report_status),
            run_every(self._stop, PROGRESS_INTERVAL_SEC, self._log_progress),
        )

    async def _worker(self) -> None:
        while not self._stop.is_set():
            task = self.scheduler.next_task()
            if task is None:
                await pause(self._stop, IDLE_SEC)
                continue

            offers_count = None
            try:
                offers_count = await collect_task(self.adapter, task, self._tracker)
            except ExchangeBlocked as exc:
                self.counters.requests_failed += 1
                log.warning(
                    "collector.blocked",
                    exchange=self.adapter.name,
                    detail=exc.detail,
                    rps=self.adapter.limiter.rps,
                    ceiling=self.adapter.limiter.ceiling,
                    pause_sec=round(self.adapter.limiter.paused_for_sec, 1),
                )
            except ExchangeError as exc:
                self.counters.requests_failed += 1
                log.warning("collector.fetch_failed", task=str(task), detail=exc.detail)
            except Exception as exc:
                # Збій БД чи баг не мають зупиняти збір інших пар (NFR-05)
                self.counters.requests_failed += 1
                log.error("collector.task_failed", task=str(task), error=repr(exc))
                await pause(self._stop, FAILURE_PAUSE_SEC)
            else:
                self.counters.requests_ok += 1
                self.counters.rows_written += offers_count
            finally:
                self.scheduler.finish(task, offers_count)

    async def update_priorities(self) -> None:
        async with SessionLocal() as session:
            tracked = await tracked_pairs(session)
        wanted = await wanted_pairs(get_redis())
        active = self._scanner.active_pairs()
        self.scheduler.set_high(self._priority_pairs | tracked | wanted | active)

    async def update_markets(self) -> None:
        """Довідник бірж раз на добу; згодом це перейде в Celery Beat (розділ 3)."""
        # Спершу відомі пари: збір не має чекати оновлення довідника чи страждати від його збою
        listed_at = await self._load_known_pairs()
        if listed_at is None or datetime.now(UTC) - listed_at > MARKETS_MAX_AGE:
            count = await refresh_markets(self.adapter)
            log.info("collector.markets_refreshed", exchange=self.adapter.name, pairs=count)
            await self._load_known_pairs()

    async def _load_known_pairs(self) -> datetime | None:
        async with SessionLocal() as session:
            markets = await find_markets(session, self.adapter.name)
            listed_at = await markets_listed_at(session, self.adapter.name)
        # Найдавніше оновлені першими: після перезапуску збір продовжує коло, а не починає спочатку
        oldest_first = sorted(markets, key=lambda market: market.buy_fetched_at or NEVER_FETCHED)
        self.scheduler.add_pairs((market.asset, market.fiat) for market in oldest_first)
        return listed_at

    async def report_status(self) -> None:
        limiter = self.adapter.limiter
        await publish_status(
            get_redis(),
            CollectorStatus(
                exchange=self.adapter.name,
                rps=limiter.rps,
                rps_ceiling=limiter.ceiling,
                limiter_state=limiter.state,
                paused_for_sec=round(limiter.paused_for_sec, 1),
                tasks=self.scheduler.counts(),
                requests_ok=self.counters.requests_ok,
                requests_failed=self.counters.requests_failed,
                rows_written=self.counters.rows_written,
                started_at=self._started_at,
                updated_at=datetime.now(UTC),
            ),
        )
        # Кулдаун — біржа тимчасово виключена з аналізу (NFR-05)
        status = ExchangeStatus.DEGRADED if limiter.state == "cooldown" else ExchangeStatus.ACTIVE
        if status != self._exchange_status:
            async with SessionLocal() as session, session.begin():
                await set_exchange_status(session, self.adapter.name, status)
            self._exchange_status = status
            log.warning("collector.exchange_status", exchange=self.adapter.name, status=status)

    async def _log_progress(self) -> None:
        log.info(
            "collector.progress",
            exchange=self.adapter.name,
            rps=self.adapter.limiter.rps,
            state=self.adapter.limiter.state,
            ok=self.counters.requests_ok,
            failed=self.counters.requests_failed,
            rows=self.counters.rows_written,
            tasks=self.scheduler.counts(),
        )


async def run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    names = [name for name in settings.exchanges if name in available_exchanges()]
    skipped = sorted(set(settings.exchanges) - set(names))
    if skipped:
        log.warning("collector.exchanges_skipped", exchanges=skipped, reason="немає адаптера")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # На Windows обробники сигналів asyncio недоступні: там зупиняє KeyboardInterrupt
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    log.info("collector.startup", exchanges=names)
    # Спільний для всіх бірж: міжбіржове порівняння потребує оголошень з усіх одразу
    tracker = MarketStatsTracker()
    scanner = OpportunityScanner(tracker, parse_pairs(settings.collector_priority_pairs))
    collectors = [
        ExchangeCollector(get_adapter(name), settings, stop, tracker, scanner) for name in names
    ]
    try:
        # Збій однієї біржі не зупиняє інші (NFR-05)
        results = await asyncio.gather(
            *(collector.run() for collector in collectors),
            run_every(stop, OPPORTUNITIES_INTERVAL_SEC, scanner.scan),
            return_exceptions=True,
        )
        for name, result in zip([*names, "opportunities"], results, strict=True):
            if isinstance(result, Exception):
                log.error("collector.exchange_crashed", exchange=name, error=repr(result))
    finally:
        await aclose_adapters()
        await close_redis()
        await engine.dispose()
        log.info("collector.shutdown")


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        asyncio.run(run())
