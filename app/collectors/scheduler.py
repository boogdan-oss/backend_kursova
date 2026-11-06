import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import IntEnum

from app.domain import Side

Pair = tuple[str, str]  # (asset, fiat)
SIDES: tuple[Side, Side] = ("BUY", "SELL")


class Priority(IntEnum):
    HIGH = 0  # watchlist, alert_rules, пари, які щойно запитали через API (FR-17)
    POPULAR = 1  # пари з великою кількістю оголошень
    REST = 2


# Частки бюджету запитів; те, що не використав один пріоритет, дістається іншим
WEIGHTS = {Priority.HIGH: 6, Priority.POPULAR: 3, Priority.REST: 1}


@dataclass(frozen=True, slots=True)
class CollectTask:
    exchange: str
    asset: str
    fiat: str
    side: Side

    @property
    def pair(self) -> Pair:
        return self.asset, self.fiat


class CollectScheduler:
    """Черга збору однієї біржі: задачі з пріоритетами, а не «пройти все кожні N секунд».

    Темп задає rate limiter. Планувальник лише вирішує, що брати наступним: пріоритет — за
    його часткою бюджету (stride scheduling), усередині пріоритету — найдавніше оновлену задачу.
    """

    def __init__(
        self,
        exchange: str,
        min_refresh_sec: float,
        popular_min_offers: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._exchange = exchange
        self._min_refresh_sec = min_refresh_sec
        self._popular_min_offers = popular_min_offers
        self._clock = clock
        self._started_at: dict[CollectTask, float] = {}
        self._offers_count: dict[CollectTask, int] = {}
        self._in_flight: set[CollectTask] = set()
        self._high: set[Pair] = set()
        self._passes = dict.fromkeys(Priority, 0.0)
        self._global_pass = 0.0

    def add_pairs(self, pairs: Iterable[Pair]) -> None:
        for asset, fiat in pairs:
            for side in SIDES:
                task = CollectTask(self._exchange, asset, fiat, side)
                self._started_at.setdefault(task, float("-inf"))

    def set_high(self, pairs: Iterable[Pair]) -> None:
        self._high = set(pairs)
        self.add_pairs(self._high)

    def priority(self, pair: Pair) -> Priority:
        if pair in self._high:
            return Priority.HIGH
        counts = (
            self._offers_count.get(CollectTask(self._exchange, *pair, side), 0) for side in SIDES
        )
        return Priority.POPULAR if max(counts) >= self._popular_min_offers else Priority.REST

    def next_task(self) -> CollectTask | None:
        """Наступна задача; None — усе вже в роботі або щойно оновлене."""
        now = self._clock()
        oldest: dict[Priority, CollectTask] = {}
        for task, started_at in self._started_at.items():
            if task in self._in_flight or now - started_at < self._min_refresh_sec:
                continue
            priority = self.priority(task.pair)
            current = oldest.get(priority)
            if current is None or started_at < self._started_at[current]:
                oldest[priority] = task
        if not oldest:
            return None

        priority = min(oldest, key=lambda p: (max(self._passes[p], self._global_pass), p))
        # max: пріоритет, що простоював, не забирає весь бюджет, коли знову з'являються задачі
        start = max(self._passes[priority], self._global_pass)
        self._passes[priority] = start + 1 / WEIGHTS[priority]
        self._global_pass = start

        task = oldest[priority]
        self._started_at[task] = now
        self._in_flight.add(task)
        return task

    def finish(self, task: CollectTask, offers_count: int | None) -> None:
        """offers_count None — збір не вдався, відома кількість оголошень лишається."""
        self._in_flight.discard(task)
        if offers_count is not None:
            self._offers_count[task] = offers_count

    def counts(self) -> dict[str, int]:
        """Кількість задач за пріоритетами — для /collector/status."""
        result = dict.fromkeys((priority.name.lower() for priority in Priority), 0)
        for task in self._started_at:
            result[self.priority(task.pair).name.lower()] += 1
        return result
